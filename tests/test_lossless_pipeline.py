from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline import config, digest, scan
from pipeline.memory import Memory


def item(n: int, tier: int, stage: str | None = None) -> dict:
    return {
        "id": f"news-{n}", "tkey": f"t:news-{n}", "source": "test", "kind": "news",
        "title": f"Malaysia construction development number {n}", "summary": "material sector event",
        "url": f"https://example.test/{n}", "published": datetime.now(timezone.utc).isoformat(),
        "tier": tier, "codes": ["3336"] if tier == 1 else [],
        "sectors": ["construction"] if tier == 2 else [], "mention": False,
        "macro": False, "impact": "normal",
    }


class LosslessPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "news.db"
        self.db_patch = patch.object(config, "NEWS_DB", self.db)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_digest_judges_every_candidate_and_spills_prompt_overflow(self):
        with Memory() as memory:
            memory.add_many(((item(n, 0), "judge") for n in range(200)), run="seed")
        calls = []

        def verdicts(_cfg, rows):
            calls.append(len(rows))
            return {row["id"]: {"codes": [], "sectors": ["construction"], "why": "sector demand"}
                    for row in rows}

        with patch.object(digest.judge, "run", side_effect=verdicts), \
             patch.object(digest.fetchers, "prices", return_value={}), \
             patch.object(digest.digest_writer, "run", return_value=[]), \
             patch.object(digest, "render_prices", return_value=""), redirect_stdout(io.StringIO()):
            digest.main([])
        self.assertEqual([60, 60, 60, 20], calls)
        with Memory() as memory:
            self.assertEqual(0, len(memory.pending("judge")))
            self.assertEqual(160, len(memory.pending("digest")))
            self.assertEqual(40, memory.stats()["by_stage"]["digested"])

    def test_digest_price_failure_leaves_item_pending(self):
        with Memory() as memory:
            memory.add(item(1, 2), "digest", run="seed")
        with patch.object(digest.fetchers, "prices", side_effect=RuntimeError("price failure")):
            with self.assertRaisesRegex(RuntimeError, "price failure"):
                digest.main([])
        with Memory() as memory:
            self.assertEqual(["news-1"], [row["id"] for row in memory.pending("digest")])

    def test_scan_price_failure_leaves_alert_pending_with_verdict(self):
        seed = item(0, 0); seed["tkey"] = "t:seed"
        target = item(1, 1)
        with Memory() as memory:
            memory.add(seed, "seen", run="seed")
        verdict = {target["id"]: {"keep": True, "type": "contract", "sentiment": "pos", "risk": False,
                                      "headline": "New contract", "summary": "Awarded.", "why": "Order book",
                                      "skip_reason": ""}}
        with patch.object(scan, "collect", return_value=[target]), \
             patch.object(scan.briefer, "run", return_value=verdict), \
             patch.object(scan.fetchers, "prices", side_effect=RuntimeError("price failure")):
            with self.assertRaisesRegex(RuntimeError, "price failure"):
                scan.main([])
        with Memory() as memory:
            pending = memory.pending("alert")
            self.assertEqual([target["id"]], [row["id"] for row in pending])
            self.assertEqual("contract", pending[0]["_verdict"]["type"])

    def test_scan_prunes_expired_rows_without_curate(self):
        """The writer expires what it wrote: a scan alone must delete rows past the retention policy."""
        from datetime import timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=config.RAW_RETENTION_DAYS + 1)).isoformat()
        seed = item(0, 0); seed["tkey"] = "t:seed"
        with Memory() as memory:
            memory.add(seed, "seen", run="seed", fetched_at=old)
            memory.add(item(2, 0), "dropped", run="seed", fetched_at=old)
            memory.add(item(3, 2), "digest", run="seed", fetched_at=old)         # pending: never pruned
            old_url = (datetime.now(timezone.utc) - timedelta(days=config.URL_RETENTION_DAYS + 1)).isoformat()
            memory.conn.execute("INSERT INTO urls(aid,url,used_at) VALUES('a','https://x.test',?)", (old_url,))
            memory.commit()
            self.assertEqual(3, memory.stats()["items"])
        with patch.object(scan, "collect", return_value=[item(1, 0)]):
            with redirect_stdout(io.StringIO()):
                scan.main([])
        with Memory() as memory:
            st = memory.stats()
            self.assertEqual({"digest": 1, "judge": 1}, st["by_stage"])   # seed + old dropped gone, pending kept
            self.assertEqual(2, st["keys"])                                # only the fresh item's two keys remain
            self.assertEqual(0, st["urls"])
            self.assertIsNotNone(st["last_prune"])


if __name__ == "__main__":
    unittest.main()
