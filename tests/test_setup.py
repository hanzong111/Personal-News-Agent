"""Setup flow: preferences -> cron schedules, stock lookup -> portfolio entries, watchlist handling,
Hermes job sync and the terminal wizard. No network, no Hermes, no writes outside a temp dir."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline import config, hold, portfolio, prefs, setup
from agents import renderer

MYT = timezone(timedelta(hours=8))

SEARCH_HITS = {
    "gamuda": [{"symbol": "5398.KL", "shortname": "GAMUDA", "longname": "Gamuda Berhad",
                "industry": "Engineering & Construction"}],
    "ijm": [{"symbol": "3336.KL", "shortname": "IJM", "longname": "IJM Corporation Berhad", "industry": "Conglomerates"},
            {"symbol": "3336WA.KL", "shortname": "IJM-WA", "longname": "IJM warrants"}],     # warrant: filtered
    "top glove": [{"symbol": "7113.KL", "shortname": "TOPGLOV", "longname": "Top Glove Corporation Bhd.",
                   "industry": "Medical Instruments & Supplies"}],
    "sunway": [{"symbol": "5211.KL", "shortname": "SUNWAY", "longname": "Sunway Berhad", "industry": "Conglomerates"},
               {"symbol": "5176.KL", "shortname": "SUNREIT", "longname": "Sunway Real Estate Investment Trust",
                "industry": "REIT—Retail"}],
}


def fake_fetch(url, params):
    if "search" in url:
        return {"quotes": SEARCH_HITS.get(params["q"].lower(), [])}
    if "4707.KL" in url:
        return {"chart": {"result": [{"meta": {"shortName": "NESTLE", "longName": "Nestlé (Malaysia) Berhad"}}]}}
    raise RuntimeError("offline")


class TempData(unittest.TestCase):
    """Point portfolio/preferences and the Hermes home at a temp dir; sectors.yaml stays the real library.
    The fake Hermes has Telegram and Discord connected."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name in ("PORTFOLIO_FILE", "PREFS_FILE"):
            p = patch.object(config, name, self.dir / Path(getattr(config, name)).name)
            p.start()
            self.addCleanup(p.stop)
        (self.dir / "channel_directory.json").write_text(json.dumps({"platforms": ["telegram", "discord"]}))
        h = patch.object(setup, "HERMES_HOME", self.dir)
        h.start()
        self.addCleanup(h.stop)
        self.cfg = config.load()


class PrefsTest(TempData):
    def test_defaults_reproduce_the_original_schedules(self):
        p = prefs.load()
        self.assertFalse(p["_exists"])
        self.assertEqual({m: prefs.schedule(p, m) for m in prefs.MESSAGES},
                         {"alerts": "*/30 8-18 * * 1-5", "digest": "25 18 * * 1-5",
                          "weekly": "55 19 * * 5", "headlines": "50 8,13,20 * * *"})
        self.assertEqual("09:00,14:00,21:00", prefs.deliver_at(p, "headlines"))
        self.assertIsNone(prefs.deliver_at(p, "alerts"))

    def test_time_parsing(self):
        for raw, want in [("7pm", "19:00"), ("6:30 pm", "18:30"), ("1930", "19:30"), ("08:05", "08:05"),
                          ("12am", "00:00"), ("9", "09:00")]:
            self.assertEqual(want, prefs.parse_time(raw), raw)
        for bad in ("25:00", "noon", "13pm"):
            with self.assertRaises(ValueError):
                prefs.parse_time(bad)

    def test_set_value_and_roundtrip(self):
        p = prefs.load()
        prefs.set_value(p, "digest.time", "7pm")
        prefs.set_value(p, "weekly", "off")
        prefs.set_value(p, "headlines.times", "8:30, 20:30")
        prefs.set_value(p, "alerts.hours", "09:00-17:00")
        prefs.save(p)
        q = prefs.load()
        self.assertTrue(q["_exists"])
        self.assertEqual("19:00", q["digest"]["time"])
        self.assertFalse(q["weekly"]["enabled"])
        self.assertEqual("20 8,20 * * *", prefs.schedule(q, "headlines"))
        self.assertEqual("*/30 9-17 * * 1-5", prefs.schedule(q, "alerts"))

    def test_invalid_value_is_rejected_and_reverted(self):
        p = prefs.load()
        with self.assertRaises(ValueError):
            prefs.set_value(p, "headlines.times", "09:00,14:30")        # one cron job needs one minute
        self.assertEqual(["09:00", "14:00", "21:00"], p["headlines"]["times"])
        with self.assertRaises(ValueError):
            prefs.set_value(p, "digest.colour", "blue")

    def test_chat_app_parsing(self):
        p = prefs.load()
        self.assertIsNone(p["deliver"])
        for raw, want in [("Telegram", "telegram"), ("Feishu / Lark", "feishu"), ("discord:#alerts", "discord:#alerts"),
                          ("Telegram: -100123", "telegram:-100123")]:
            prefs.set_value(p, "deliver", raw)
            self.assertEqual(want, p["deliver"], raw)
        with self.assertRaises(ValueError):
            prefs.set_value(p, "deliver", "my phone!")
        self.assertEqual("telegram:-100123", p["deliver"])               # bad value leaves the old one
        prefs.save(p)
        self.assertEqual("telegram:-100123", prefs.load()["deliver"])
        self.assertEqual("📨 Sent to — Telegram (-100123)", prefs.describe(p)[0])

    def test_lead_crossing_midnight_shifts_the_day(self):
        p = prefs.load()
        prefs.set_value(p, "digest.time", "00:02")
        self.assertEqual("57 23 * * 0-4", prefs.schedule(p, "digest"))
        prefs.set_value(p, "weekly.day", "sun")
        prefs.set_value(p, "weekly.time", "00:00")
        self.assertEqual("55 23 * * 6", prefs.schedule(p, "weekly"))


class HoldPrefsTest(TempData):
    def setUp(self):
        super().setUp()
        self.addCleanup(os.environ.pop, "BURSA_DELIVER_AT", None)

    def _hold(self, message, when="2026-09-24 18:56:00"):
        now = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MYT)
        return hold.until_target(message, now=now, sleep=lambda s: None)

    def test_manual_runs_never_hold(self):
        os.environ.pop("BURSA_DELIVER_AT", None)
        self.assertEqual(0.0, self._hold("digest", "2026-09-24 18:26:00"))

    def test_prefs_times_used_when_wrapper_asks(self):
        p = prefs.load()
        prefs.set_value(p, "digest.time", "19:00")
        prefs.save(p)
        os.environ["BURSA_DELIVER_AT"] = "prefs"
        self.assertAlmostEqual(240, self._hold("digest"), delta=1)

    def test_prefs_file_overrides_old_wrapper_times(self):
        p = prefs.load()
        prefs.set_value(p, "digest.time", "19:00")
        prefs.save(p)
        os.environ["BURSA_DELIVER_AT"] = "18:30"                         # pre-setup wrapper
        self.assertAlmostEqual(240, self._hold("digest"), delta=1)

    def test_prefs_mode_without_file_uses_defaults(self):
        os.environ["BURSA_DELIVER_AT"] = "prefs"
        self.assertAlmostEqual(240, self._hold("digest", "2026-09-24 18:26:00"), delta=1)


class LookupTest(TempData):
    def test_find_by_name_builds_a_ready_entry(self):
        (s,) = portfolio.find("gamuda", self.cfg, fetch=fake_fetch)
        self.assertEqual(("5398", "GAMUDA", "Gamuda Bhd", "construction"), (s.code, s.short, s.name, s.sector))
        self.assertEqual(["Gamuda"], s.aliases)
        self.assertEqual(['"Gamuda"'], s.queries)

    def test_short_names_match_whole_words_and_warrants_are_skipped(self):
        (s,) = portfolio.find("ijm", self.cfg, fetch=fake_fetch)
        self.assertEqual(["IJM "], s.aliases)

    def test_sector_suggestion_uses_industry(self):
        (s,) = portfolio.find("top glove", self.cfg, fetch=fake_fetch)
        self.assertEqual("gloves", s.sector)
        self.assertEqual(["Top Glove", "TOPGLOV"], s.aliases)

    def test_code_falls_back_to_chart_lookup(self):
        (s,) = portfolio.find("4707", self.cfg, fetch=fake_fetch)
        self.assertEqual(("4707", "NESTLE", "other"), (s.code, s.short, s.sector))
        self.assertEqual(["Nestlé", "Nestle"], s.aliases)

    def test_longer_phrasing_retries_shorter(self):
        hits = portfolio.find("sunway reit", self.cfg, fetch=fake_fetch)
        self.assertEqual(["5211", "5176"], [h.code for h in hits])
        self.assertEqual("reit", hits[1].sector)
        self.assertEqual(["Sunway REIT", "SUNREIT"], hits[1].aliases)

    def test_network_failure_returns_nothing(self):
        self.assertEqual([], portfolio.find("9999", self.cfg, fetch=fake_fetch))


class PortfolioFileTest(TempData):
    def test_add_move_remove_and_roundtrip(self):
        data = portfolio.read()
        gamuda, = portfolio.find("gamuda", self.cfg, fetch=fake_fetch)
        glove, = portfolio.find("top glove", self.cfg, fetch=fake_fetch)
        self.assertEqual("added GAMUDA to holdings", portfolio.add(data, gamuda))
        self.assertEqual("added TOPGLOV to watchlist", portfolio.add(data, glove, watch=True))
        data["holdings"][0]["qty"] = 1000                                # user extras survive rewrites
        portfolio.write(data)

        cfg = config.load()
        self.assertEqual(["GAMUDA"], [h.short for h in cfg.owned])
        self.assertEqual(["TOPGLOV"], [h.short for h in cfg.watched])
        self.assertEqual("👀 TOPGLOV", cfg.by_code("7113").label)
        self.assertIn("qty: 1000", config.PORTFOLIO_FILE.read_text())

        data = portfolio.read()
        self.assertEqual("moved TOPGLOV to holdings", portfolio.add(data, glove))
        self.assertEqual("removed GAMUDA from holdings", portfolio.remove(data, "gamuda"))
        self.assertIsNone(portfolio.remove(data, "NOPE"))
        portfolio.write(data)
        self.assertTrue(config.PORTFOLIO_FILE.with_suffix(".yaml.bak").exists())
        self.assertEqual(["TOPGLOV"], [h.short for h in config.load().owned])

    def test_missing_portfolio_loads_empty(self):
        self.assertEqual([], config.load().holdings)

    def test_sector_heading_separates_owned_and_watched(self):
        data = portfolio.read()
        portfolio.add(data, portfolio.find("gamuda", self.cfg, fetch=fake_fetch)[0])
        portfolio.add(data, portfolio.find("ijm", self.cfg, fetch=fake_fetch)[0], watch=True)
        portfolio.write(data)
        self.assertEqual("you hold: GAMUDA · watching 👀: IJM", config.load().touches("construction"))


class ApplyTest(TempData):
    JOBS = {"jobs": [
        {"id": "a1", "name": "bursa-scan", "schedule": {"expr": "*/30 8-18 * * 1-5"}, "enabled": True, "state": "scheduled"},
        {"id": "d1", "name": "bursa-digest", "schedule": {"expr": "25 18 * * 1-5"}, "enabled": True, "state": "scheduled"},
        {"id": "w1", "name": "bursa-weekly", "schedule": {"expr": "55 19 * * 5"}, "enabled": True, "state": "scheduled"},
        {"id": "h1", "name": "malaysia-news", "schedule": {"expr": "50 8,13,20 * * *"}, "enabled": True,
         "state": "paused", "paused_at": "2026-09-01T00:00:00"},
    ]}

    def setUp(self):
        super().setUp()
        self.jobs_file = self.dir / "jobs.json"
        self.jobs_file.write_text(json.dumps(self.JOBS))

    def test_plan_changes_only_what_differs(self):
        p = prefs.load()
        prefs.set_value(p, "digest.time", "19:00")
        prefs.set_value(p, "weekly", "off")
        cmds, _ = setup.plan_apply(p, setup._jobs(self.jobs_file))
        self.assertEqual([["hermes", "cron", "edit", "d1", "--schedule", "55 18 * * 1-5"],
                          ["hermes", "cron", "pause", "w1"],
                          ["hermes", "cron", "resume", "h1"]], cmds)

    def test_apply_runs_hermes_and_dry_run_does_not(self):
        p = prefs.load()
        ran = []
        run = lambda c, **kw: ran.append(c) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        out = setup.apply(p, dry_run=True, jobs_file=self.jobs_file, run=run)
        self.assertEqual([], ran)
        self.assertIn("malaysia-news: resume", out)
        setup.apply(p, jobs_file=self.jobs_file, run=run)
        self.assertEqual([["hermes", "cron", "resume", "h1"]], ran)

    def test_chat_app_moves_every_project_job(self):
        jobs = setup._jobs(self.jobs_file)
        jobs["bursa-ops"] = {"id": "o1", "name": "bursa-ops", "schedule": {"expr": "*/30 * * * *"}, "deliver": "feishu"}
        jobs["someone-elses-job"] = {"id": "x1", "name": "someone-elses-job", "deliver": "feishu"}
        for j in jobs.values():
            j.setdefault("deliver", "feishu")
        p = prefs.load()
        cmds, _ = setup.plan_apply(p, jobs)
        self.assertFalse(any("--deliver" in c for c in cmds))              # not chosen yet: leave delivery alone
        prefs.set_value(p, "deliver", "discord")
        cmds, _ = setup.plan_apply(p, jobs)
        moved = {c[3] for c in cmds if "--deliver" in c}
        self.assertEqual({"a1", "d1", "w1", "h1", "o1"}, moved)            # all ours, never x1

    def test_target_falls_back_to_first_connected_app(self):
        p = prefs.load()
        self.assertEqual("telegram", setup.target(p))
        prefs.set_value(p, "deliver", "discord")
        self.assertEqual("discord", setup.target(p))

    def test_send_test_uses_hermes_send(self):
        calls = []
        ok = lambda c, **kw: calls.append(c) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        self.assertTrue(setup.send_test("discord", run=ok).startswith("✓"))
        self.assertEqual(["hermes", "send", "-q", "-t", "discord"], calls[0][:5])
        bad = lambda c, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "discord not configured"})()
        self.assertIn("discord not configured", setup.send_test("discord", run=bad))

    def test_missing_job_is_reported(self):
        cmds, notes = setup.plan_apply(prefs.load(), {})
        self.assertEqual([], cmds)
        self.assertEqual(4, sum("not found" in n for n in notes))


class WizardTest(TempData):
    def test_full_run_writes_portfolio_and_preferences(self):
        answers = iter([
            "gamuda", "",            # holding, keep suggested sector
            "",                      # done with holdings
            "sunway reit", "2", "",  # watchlist: pick SUNREIT, keep sector
            "",                      # done with watchlist
            "2",                     # chat app: #2 of the connected apps = discord
            "y", "9-17", "weekdays",  # alerts
            "y", "7pm", "daily",     # digest
            "n",                     # no weekly
            "y", "08:00,20:00",      # headlines
            "n",                     # no Jev
            "y",                     # apply
            "y",                     # send a test message
        ])
        said, applied, sent = [], [], []
        w = setup.Wizard(ask=lambda _q: next(answers), say=said.append, apply_fn=lambda p: applied.append(p) or ["ok"],
                         send_fn=lambda d: sent.append(d) or "✓ sent")
        with patch.object(portfolio, "_get_json", fake_fetch):
            self.assertEqual(0, w.run())
        cfg, p = config.load(), prefs.load()
        self.assertEqual(["GAMUDA"], [h.short for h in cfg.owned])
        self.assertEqual(["SUNREIT"], [h.short for h in cfg.watched])
        self.assertEqual(("19:00", "daily"), (p["digest"]["time"], p["digest"]["days"]))
        self.assertFalse(p["weekly"]["enabled"])
        self.assertEqual([9, 17], p["alerts"]["hours"])
        self.assertEqual(["08:00", "20:00"], p["headlines"]["times"])
        self.assertEqual("discord", p["deliver"])
        self.assertEqual(1, len(applied))
        self.assertEqual(["discord"], sent)

    def test_bad_time_is_asked_again(self):
        answers = iter(["gamuda", "", "", "", "whatsapp", "n", "y", "half six", "6:30pm", "weekdays", "n", "n", "n", "n", "n"])
        said = []
        w = setup.Wizard(ask=lambda _q: next(answers), say=said.append, apply_fn=lambda p: ["ok"], send_fn=lambda d: "")
        with patch.object(portfolio, "_get_json", fake_fetch):
            self.assertEqual(0, w.run())
        self.assertTrue(any("not a time" in s for s in said))
        self.assertTrue(any("WhatsApp isn't connected" in s for s in said))    # chosen app not in Hermes yet
        self.assertEqual("18:30", prefs.load()["digest"]["time"])


class WatchlistAlertTest(TempData):
    def test_alert_marks_watchlist_stock(self):
        data = portfolio.read()
        portfolio.add(data, portfolio.find("ijm", self.cfg, fetch=fake_fetch)[0], watch=True)
        portfolio.write(data)
        cfg = config.load()
        items = [{"id": "x1", "codes": ["3336"], "published": "2026-09-24T10:00:00+08:00", "source": "The Edge",
                  "url": "https://example.com/a", "impact": "high"}]
        verdicts = {"x1": {"keep": True, "type": "contract", "risk": False, "sentiment": "pos",
                           "headline": "IJM wins RM1bn job", "summary": "", "why": ""}}
        text = renderer.render_alert(cfg, items, verdicts, {})
        self.assertIn("**👀 IJM** (3336)", text)


if __name__ == "__main__":
    unittest.main()
