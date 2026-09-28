from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline import config, curate
from pipeline.memory import Memory


class CuratorTest(unittest.TestCase):
    def test_same_day_duplicates_share_story_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "news.db"
            now = datetime.now(timezone.utc).isoformat()
            rows = [
                {"id": "a", "tkey": "t:a", "source": "one", "kind": "news",
                 "title": "Gamuda wins RM1.8 billion Australian rail contract", "summary": "Sydney project",
                 "url": "https://one.test/a", "published": now, "tier": 1, "codes": ["5398"],
                 "sectors": ["construction"], "mention": False, "macro": False, "impact": "normal"},
                {"id": "b", "tkey": "t:b", "source": "two", "kind": "news",
                 "title": "Gamuda wins RM1.8 billion Australian rail contract today", "summary": "Same Sydney project",
                 "url": "https://two.test/b", "published": now, "tier": 1, "codes": ["5398"],
                 "sectors": ["construction"], "mention": False, "macro": False, "impact": "normal"},
            ]
            with patch.object(config, "NEWS_DB", db):
                with Memory() as memory:
                    memory.add_many(((row, "alerted") for row in rows), run="seed")
                with redirect_stdout(io.StringIO()) as output:
                    curate.main(["--no-llm"])
                self.assertEqual("", output.getvalue())
                with Memory() as memory:
                    stored = memory.delivered()
                    self.assertEqual(1, memory.stats()["stories"])
                    self.assertEqual(stored[0]["story_id"], stored[1]["story_id"])
                    self.assertIn("5398", memory.notes_for(["5398"]))


if __name__ == "__main__":
    unittest.main()
