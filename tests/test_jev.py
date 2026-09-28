"""Jev mode: the switch, the fallback to the original rules, and each wiring point, with a fake Jev.

No network and no API key: `agents.jev` functions are patched to return canned judgments.
"""
import unittest
from unittest.mock import patch

from agents import jev
from pipeline import config, news_fetch, prefs, scan


def _item(id_, title, codes, published="2026-09-28T10:00:00+08:00", kind="news"):
    return {"id": id_, "title": title, "codes": codes, "published": published, "kind": kind,
            "source": "Test", "summary": ""}


class SwitchTest(unittest.TestCase):
    def setUp(self):
        jev._state.clear()
        self.addCleanup(jev._state.clear)

    def test_off_by_default_and_nothing_is_asked(self):
        self.assertFalse(prefs.load()["jev"])
        self.assertFalse(jev.enabled())
        self.assertEqual([None, None], jev.same_event([({}, {}), ({}, {})]))
        self.assertEqual([None], jev.roles([("t", None)]))

    def test_on_without_a_key_stays_off(self):
        p = prefs.load()
        prefs.set_value(p, "jev", "on")
        with patch.object(prefs, "load", return_value=p):
            ok, why = jev.status()
            self.assertFalse(ok)
            self.assertIn("TYPESAFE_API_KEY", why)
            self.assertFalse(jev.enabled())

    def test_preference_roundtrip(self):
        p = prefs.load()
        prefs.set_value(p, "jev", "yes")
        self.assertTrue(p["jev"])
        self.assertIn("🧠 Jev smart filtering — on", prefs.describe(p))


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()          # fixture portfolio: IJM 3336, CYPARK 5184, KPJ 5878

    def test_relevance_moves_quoted_or_listed_stocks_to_the_digest(self):
        alerts = [_item("a", "IJM wins RM1bn highway job", ["3336"]),
                  _item("b", "Trading ideas: IJM, Dialog, Frontken", ["3336"]),
                  _item("c", "Cypark files quarterly report", ["5184"], kind="announcement"),
                  _item("d", "IJM mentioned somewhere", ["3336"])]
        answers = {"IJM wins RM1bn highway job": ("subject", 0.99),
                   "Trading ideas: IJM, Dialog, Frontken": ("passing_mention", 0.95),
                   "IJM mentioned somewhere": ("passing_mention", 0.4)}         # unsure: keep the alert
        with patch.object(jev, "roles", side_effect=lambda pairs: [answers[t] for t, _ in pairs]) as roles:
            keep, demote = scan._jev_relevance(self.cfg, alerts)
        self.assertEqual(["a", "c", "d"], [i["id"] for i in keep])
        self.assertEqual(["b"], [i["id"] for i in demote])
        asked = [t for call in roles.call_args_list for t, _ in call.args[0]]
        self.assertNotIn("Cypark files quarterly report", asked)               # filings are never questioned

    def test_repeats_against_recent_alerts_and_within_the_batch(self):
        recent = [{"title": "IJM bags RM1bn highway contract", "codes": ["3336"]}]
        batch = [_item("x", "IJM secures RM1 billion highway job", ["3336"], "2026-09-28T10:00:00+08:00"),
                 _item("y", "KPJ opens new hospital in Johor", ["5878"], "2026-09-28T10:05:00+08:00"),
                 _item("z", "KPJ launches Johor hospital", ["5878"], "2026-09-28T10:30:00+08:00")]
        same = {("IJM secures RM1 billion highway job", "IJM bags RM1bn highway contract"): 2.0,
                ("KPJ launches Johor hospital", "KPJ opens new hospital in Johor"): 1.9}
        with patch.object(jev, "same_event",
                          side_effect=lambda pairs, role="": [same.get((a["title"], b["title"]), 0.1) for a, b in pairs]):
            rep = scan._jev_repeats(batch, recent)
        self.assertEqual({"x": "IJM bags RM1bn highway contract", "z": "KPJ opens new hospital in Johor"}, rep)

    def test_pairs_jev_cannot_answer_fall_back_to_the_word_rule(self):
        recent = [{"title": "IJM wins RM1bn highway job in Perak", "codes": ["3336"]}]
        batch = [_item("x", "IJM wins RM1bn highway job in Perak state", ["3336"])]
        with patch.object(jev, "same_event", side_effect=lambda pairs, role="": [None] * len(pairs)):
            self.assertIn("x", scan._jev_repeats(batch, recent))                 # rule: word overlap >= 0.5


class ClusterTest(unittest.TestCase):
    def _story(self, title, src="X"):
        from datetime import datetime, timezone
        return {"title": title, "src": src, "ts": datetime(2026, 9, 28, tzinfo=timezone.utc), "reports": 1}

    def test_jev_merges_across_languages_and_splits_false_rule_merges(self):
        items = [self._story("Siti Hasmah, wife of ex-PM Mahathir, dies at 100"),
                 self._story("Isteri mantan PM Mahathir, Siti Hasmah, meninggal dunia"),
                 self._story("Kelantan flood warning issued for Pasir Mas rivers"),
                 self._story("Kelantan flood warning issued for Pasir Mas schools")]    # rule merges these

        def fake(pairs, role=""):
            scores = []
            for a, b in pairs:
                both = {a["title"][:5], b["title"][:5]}
                scores.append(2.0 if both == {"Siti ", "Ister"} else 0.2)
            return scores
        with patch.object(jev, "enabled", return_value=True), patch.object(jev, "same_event", side_effect=fake):
            out = news_fetch.cluster(items)
        groups = sorted(sorted([s["title"][:5]] + [m["title"][:5] for m in s["related"]]) for s in out)
        self.assertIn(["Ister", "Siti "], groups)                              # same story, two languages
        self.assertEqual(3, len(out))                                          # Jev split the two flood items
        self.assertIn(["Kelan"], groups)

    def test_without_jev_the_rule_clusters_as_before(self):
        items = [self._story("Kelantan flood warning issued for Pasir Mas rivers"),
                 self._story("Kelantan flood warning issued for Pasir Mas schools"),
                 self._story("Siti Hasmah, wife of ex-PM Mahathir, dies at 100"),
                 self._story("Isteri mantan PM Mahathir, Siti Hasmah, meninggal dunia")]
        with patch.object(jev, "enabled", return_value=False):
            out = news_fetch.cluster(items)
        self.assertEqual(2, len(out))                  # the rule merges the flood items and the two languages


if __name__ == "__main__":
    unittest.main()


class JudgeTest(unittest.TestCase):
    def test_jev_answers_are_used_and_the_rest_go_to_haiku(self):
        from pipeline import digest
        cfg = config.load()
        cands = [{"id": "p", "title": "Govt cuts solar grid charge", "summary": ""},
                 {"id": "q", "title": "Celebrity wedding in Langkawi", "summary": ""},
                 {"id": "r", "title": "Something Jev could not answer", "summary": ""}]
        answers = [{"codes": [], "sectors": ["solar"]}, {"codes": [], "sectors": []}, None]

        class Mem:
            def __init__(self):
                self.saved = {}

            def judge_result(self, id_, verdict, run):
                self.saved[id_] = verdict
        mem = Mem()
        with patch.object(jev, "relevance", return_value=answers):
            rescued, left = digest._jev_judge(cfg, cands, mem, "run", persist=True)
        self.assertEqual(["p"], [c["id"] for c in rescued])
        self.assertEqual(["solar"], rescued[0]["sectors"])
        self.assertEqual(["r"], [c["id"] for c in left])                        # only this one reaches Haiku
        self.assertEqual({"p": {"codes": [], "sectors": ["solar"], "why": ""}, "q": None}, mem.saved)
