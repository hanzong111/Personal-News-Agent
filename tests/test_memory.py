from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.memory import Memory


def item(n: int, title: str = "Gamuda wins a rail contract") -> dict:
    return {
        "id": f"item-{n}", "tkey": f"t:key-{n}", "source": "test", "kind": "news",
        "title": title, "summary": "summary", "url": f"https://example.test/{n}",
        "published": datetime.now(timezone.utc).isoformat(), "tier": 1, "codes": ["5398"],
        "sectors": ["construction"], "mention": False, "macro": False, "impact": "normal",
    }


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(self.root / "news.db")

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_lifecycle_verdict_story_and_notes(self):
        self.assertTrue(self.mem.add(item(1), "alert", run="scan-1"))
        self.assertTrue(self.mem.known("item-1", "t:key-1"))
        self.assertEqual(["item-1"], [x["id"] for x in self.mem.pending("alert")])
        verdict = {"keep": True, "type": "contract", "sentiment": "pos", "risk": False,
                   "headline": "Gamuda wins rail contract", "summary": "A concrete award.",
                   "why": "Adds to order book", "skip_reason": ""}
        self.mem.save_verdicts({"item-1": verdict})
        self.mem.advance(["item-1"], "alerted", run="scan-1")
        delivered = self.mem.delivered()
        self.assertEqual("contract", delivered[0]["_verdict"]["type"])
        sid = self.mem.create_story(delivered[0], "contract")
        self.assertTrue(sid.startswith("story-"))
        self.mem.set_note("5398", "holding", "- Today: rail contract")
        self.assertIn("5398", self.mem.notes_for(["5398"]))
        self.assertEqual(1, len(self.mem.recent_alerts()))

    def test_judge_and_prune(self):
        self.mem.add(item(2), "judge")
        self.mem.judge_result("item-2", {"codes": [], "sectors": ["construction"], "why": "input costs"})
        self.assertEqual("digest", self.mem.pending("digest")[0]["stage"])
        self.mem.advance(["item-2"], "dropped", "test")
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.mem.conn.execute("UPDATE items SET fetched_at=?", (old,))
        self.mem.conn.execute("UPDATE item_keys SET first_seen=?", (old,))
        self.mem.conn.commit()
        plan = self.mem.prune(dry_run=True)
        self.assertEqual(1, plan["raw"])
        self.assertGreaterEqual(plan["keys"], 1)
        self.mem.prune()
        self.assertEqual(0, self.mem.stats()["items"])

    def test_url_cache(self):
        self.mem.urls_put({"a": "https://publisher.test/a"})
        self.assertEqual({"a": "https://publisher.test/a"}, self.mem.urls_get(["a"]))

    def test_idempotent_legacy_migration(self):
        legacy = self.root / "legacy"; legacy.mkdir()
        con = sqlite3.connect(legacy / "seen.db")
        con.execute("CREATE TABLE seen(id TEXT PRIMARY KEY,source TEXT,title TEXT,first_seen TEXT)")
        now = datetime.now(timezone.utc).isoformat()
        con.executemany("INSERT INTO seen VALUES(?,?,?,?)", [
            ("item-3", "test", "Title", now), ("t:key-3", "test", "Title", now)])
        con.commit(); con.close()
        queued = item(3); queued["tier"] = 2
        (legacy / "digest_queue.jsonl").write_text(json.dumps(queued) + "\n")
        (legacy / "judge_queue.jsonl").write_text("")
        (legacy / "alerted.jsonl").write_text(json.dumps(
            {"ts": datetime.now(timezone.utc).timestamp(), "codes": ["5398"], "title": "Prior alert", "terms": ["prior", "alert"]}) + "\n")
        con = sqlite3.connect(legacy / "gnews_urls.db")
        con.execute("CREATE TABLE urls(aid TEXT PRIMARY KEY,url TEXT NOT NULL)")
        con.execute("INSERT INTO urls VALUES('aid-1','https://publisher.test/1')")
        con.commit(); con.close()

        first = self.mem.migrate(legacy, rename=False)
        item_count = self.mem.stats()["items"]
        second = self.mem.migrate(legacy, rename=False)
        self.assertEqual((2, 2), first["checks"]["seen"])
        self.assertEqual((1, 1), first["checks"]["pending"])
        self.assertEqual(item_count, self.mem.stats()["items"])
        self.assertEqual(first["checks"], second["checks"])
        self.assertEqual(1, len(self.mem.pending("digest")))
        self.assertEqual({"aid-1": "https://publisher.test/1"}, self.mem.urls_get(["aid-1"]))


if __name__ == "__main__":
    unittest.main()
