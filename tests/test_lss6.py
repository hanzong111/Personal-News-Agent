from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline import config, lss6


def _news(title, url="https://x/1"):
    return {"title": title, "source": "The Edge Malaysia (via Google News)", "published": "2026-09-30T09:15:00+08:00", "url": url}


class ClassifyTest(unittest.TestCase):
    def test_result_wording(self):
        self.assertEqual("result", lss6.classify("Energy Commission names LSS6 shortlisted bidders"))
        self.assertEqual("result", lss6.classify("Cypark bags 500MW in LSS6"))
        self.assertEqual("result", lss6.classify("Senarai pembida berjaya LSS6 diumumkan"))

    def test_speculation_is_a_heads_up_and_noise_is_ignored(self):
        self.assertEqual("heads-up", lss6.classify("Cypark, Sunview tipped to win LSS6 contracts"))
        self.assertEqual("heads-up", lss6.classify("Successful LSS6 Bidders Should Know Latest By September"))
        self.assertIsNone(lss6.classify("Govt launches LSS6, expecting RM13b investment"))
        self.assertIsNone(lss6.classify("Gamuda wins RM1.8bil Australian road contract"))   # no LSS context


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.bursa, self.news, self.st = [], [], {"snippets": [], "links": []}

    def _run(self, state):
        with patch.object(lss6, "_bursa", side_effect=lambda kw: self.bursa if kw == "LSS6" else []), \
             patch.object(lss6.fetchers, "gnews", side_effect=lambda q, *a, **k: self.news), \
             patch.object(lss6, "_st_fingerprint",
                          side_effect=lambda url: self.st if url.endswith("/newsroom") else {"snippets": [], "links": []}):
            return lss6.check(self.cfg, state)

    def test_first_run_records_silently_then_new_items_alert(self):
        self.news = [_news("Cypark, Sunview tipped to win LSS6 contracts")]
        alerts, state = self._run({})
        self.assertEqual([], alerts)
        self.bursa = [{"id": "bursa:1", "company": "CYPARK RESOURCES BERHAD", "when": "30 Sep 2026",
                       "title": "NOTIFICATION OF SHORTLISTED BIDDER FOR LARGE SCALE SOLAR (LSS6)", "url": "https://b/1"}]
        self.news.append(_news("Energy Commission unveils LSS6 shortlisted bidders", "https://x/2"))
        self.st = {"snippets": ["Senarai Pembida Berjaya LSS6 …"], "links": ["/lss6-results"]}
        alerts, state = self._run(state)
        text = "\n".join(alerts)
        self.assertEqual(3, len(alerts))
        self.assertIn("Bursa filing", text)
        self.assertIn("CYPARK", text)                         # holding named in the filing
        self.assertIn("LSS6 result?", text)
        self.assertIn("Energy Commission site mentions LSS", text)
        self.assertNotIn("tipped", text)                      # baseline item never re-alerts
        self.assertEqual([], self._run(state)[0])             # nothing new -> silent

    def test_all_sources_down_fails_loudly(self):
        with patch.object(lss6, "_bursa", side_effect=RuntimeError), \
             patch.object(lss6.fetchers, "gnews", side_effect=RuntimeError), \
             patch.object(lss6, "_st_fingerprint", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                lss6.check(self.cfg, {"seen": []})


if __name__ == "__main__":
    unittest.main()
