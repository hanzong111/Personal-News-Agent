from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from agents import digest_writer
from agents.renderer import render_digest
from pipeline import config

CRESS = {"id": "k1", "source": "TheEdge (via KLSE Screener)", "title": "Renewable energy stocks gain amid CRESS",
         "summary": "Cypark leads", "published": "2026-09-23T10:06:00+08:00", "codes": ["5184"],
         "sectors": ["solar"], "mention": True,
         "url": "https://www.klsescreener.com/v2/news/view/1796153/renewable-energy-stocks-gain-amid-cress"}
CRESS_ZH = {**CRESS, "id": "k2", "published": "2026-09-23T10:25:00+08:00",
            "url": "https://www.klsescreener.com/v2/news/view/1796163/cress"}
BROADCOM = {"id": "e1", "source": "The Edge Malaysia", "title": "China surveys Broadcom switch use",
            "summary": "", "published": "2026-09-23T15:39:00+08:00", "codes": [], "sectors": ["construction"],
            "url": "https://theedgemalaysia.com/node/819054"}


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_links_come_from_the_items_named_not_from_the_model(self):
        stories = [{"section": "solar", "item_ids": ["k1", "k2"], "sentiment": "pos",
                    "headline": "CRESS package lifts renewable stocks", "summary": "Rally.", "touches": "CYPARK"}]
        text = render_digest(self.cfg, [CRESS, CRESS_ZH, BROADCOM], stories, {}, datetime(2026, 9, 23, 18, 30))
        self.assertIn("1796153", text)
        self.assertIn("1796163", text)
        self.assertNotIn("819054", text)                  # an unrelated item's URL can't leak in
        self.assertIn("🕒 23 Sep 2026 10:06/10:25", text)
        self.assertIn("[TheEdge via KLSE Screener](https://www.klsescreener.com/v2/news/view/1796153", text)

    def test_many_reports_collapse_to_a_range_and_english_links_first(self):
        zh = {**CRESS, "id": "z", "title": "CRESS附加费减免 可再生能源股上涨", "published": "2026-09-21T19:09:00+08:00",
              "url": "https://www.klsescreener.com/zh"}
        more = [{**CRESS, "id": f"m{n}", "published": f"2026-09-22T1{n}:00:00+08:00", "url": f"https://x/{n}"} for n in range(3)]
        stories = [{"section": "solar", "item_ids": ["z", "k1", *[m["id"] for m in more]], "sentiment": "pos",
                    "headline": "H", "summary": "", "touches": ""}]
        text = render_digest(self.cfg, [zh, CRESS, *more], stories, {}, datetime(2026, 9, 23, 18, 30))
        self.assertIn("🕒 21 Sep 19:09 → 23 Sep 10:06 (5 reports)", text)
        self.assertNotIn("klsescreener.com/zh", text)       # English copies fill the two link slots first

    def test_writer_drops_unknown_and_reused_ids(self):
        reply = {"stories": [
            {"section": "solar", "item_ids": ["k1", "ghost"], "sentiment": "pos", "headline": "A", "summary": "", "touches": ""},
            {"section": "nope", "item_ids": ["k1"], "sentiment": "weird", "headline": "B", "summary": "", "touches": ""},
            {"section": "construction", "item_ids": ["e1"], "sentiment": "weird", "headline": "C", "summary": "", "touches": ""},
        ]}
        with patch("agents.llm.call_json", return_value=reply):
            stories = digest_writer.run(self.cfg, [CRESS, BROADCOM])
        self.assertEqual([["k1"], ["e1"]], [s["item_ids"] for s in stories])   # ghost dropped, k1 not reused
        self.assertEqual("neu", stories[1]["sentiment"])

    def test_unheld_sector_story_renders_under_macro(self):
        stories = [{"section": "banking", "item_ids": ["e1"], "sentiment": "neu", "headline": "Bank thing",
                    "summary": "", "touches": ""}]
        text = render_digest(self.cfg, [BROADCOM], stories, {}, datetime(2026, 9, 23, 18, 30))
        self.assertIn("Macro / market", text)
        self.assertIn("Bank thing", text)


if __name__ == "__main__":
    unittest.main()
