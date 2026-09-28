from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import costs


def _state(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.executescript("""
        create table sessions (id text, source text, parent_session_id text, started_at real);
        create table session_model_usage (session_id text, model text, task text, api_call_count int,
            input_tokens int, output_tokens int, cache_read_tokens int, cache_write_tokens int);
        create table messages (session_id text, role text, display_kind text, timestamp real);
    """)
    return c


def _usage(c, sid, model, task, calls, inp, out, cr, cw):
    c.execute("delete from session_model_usage where session_id=? and model=? and task=?", (sid, model, task))
    c.execute("insert into session_model_usage values (?,?,?,?,?,?,?,?)", (sid, model, task, calls, inp, out, cr, cw))
    c.commit()


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state = _state(d / "state.db")
        self.patches = [patch.object(costs, "STATE_DB", str(d / "state.db")),
                        patch.object(costs, "LEDGER", d / "costs.db"),
                        patch.object(costs, "_job_names", return_value={"d1b82acc712f": "bursa-digest"}),
                        patch.object(costs, "_pipeline_roles", return_value={"s_pipe": "pipeline:news/classifier"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.state.close()
        self.tmp.cleanup()

    def _ledger(self):
        return sqlite3.connect(costs.LEDGER).execute(
            "select category, model, task, calls, inp, out, cr, cw, round(usd,6), session_id from usage "
            "order by category").fetchall()

    def test_baseline_then_deltas_by_category(self):
        s = self.state
        s.executemany("insert into sessions values (?,?,?,?)", [
            ("chat1", "feishu", None, 0), ("cron_d1b82acc712f_x", "cron", None, 0),
            ("s_pipe", "oneshot", None, 0), ("sub1", "subagent", "chat1", 0)])
        _usage(s, "chat1", "claude-opus-5", "", 100, 10, 1000, 5_000_000, 100_000)
        costs.collect(refresh_rate=False, now=1000.0)                       # baseline: nothing counted
        self.assertEqual([], self._ledger())

        _usage(s, "chat1", "claude-opus-5", "", 102, 12, 1500, 5_100_000, 110_000)   # grows by 2 calls
        _usage(s, "chat1", "claude-sonnet-5", "compression", 1, 20_000, 1_000, 0, 0)
        _usage(s, "cron_d1b82acc712f_x", "claude-sonnet-5", "", 1, 3, 1364, 0, 34_065)
        _usage(s, "s_pipe", "claude-haiku-4-5", "", 1, 5, 3000, 0, 3000)
        _usage(s, "sub1", "claude-opus-5", "", 1, 0, 100, 0, 1000)
        costs.collect(refresh_rate=False, now=2000.0)
        ledger = self._ledger()
        rows = {(r[0], r[2]): r for r in ledger if r[9] != "sub1"}
        self.assertEqual((2, 2, 500, 100_000, 10_000), rows[("chat:feishu", "")][3:8])
        # 2*5 + 500*25 + 100k*0.5 + 10k*6.25 per MTok
        self.assertAlmostEqual((2 * 5 + 500 * 25 + 100_000 * .5 + 10_000 * 6.25) / 1e6, rows[("chat:feishu", "")][8], 6)
        self.assertIn(("chat:feishu", "compression"), rows)
        self.assertIn(("cron:bursa-digest", ""), rows)
        self.assertIn(("pipeline:news/classifier", ""), rows)
        self.assertEqual("chat:feishu", next(r[0] for r in ledger if r[9] == "sub1"))   # subagent folds into its chat

        costs.collect(refresh_rate=False, now=3000.0)                       # nothing changed -> nothing added
        self.assertEqual(5, len(self._ledger()))

    def test_split_billing_rows_are_summed_not_flip_flopped(self):
        s = self.state
        s.execute("insert into sessions values ('c','feishu',NULL,0)")
        for calls in (96, 2):   # Hermes: same session+model+task, two billing routes
            s.execute("insert into session_model_usage values ('c','gpt-5.6-sol','',?,1,1,1,1)", (calls,))
        s.commit()
        costs.collect(refresh_rate=False, now=1000.0)
        costs.collect(refresh_rate=False, now=2000.0)
        costs.collect(refresh_rate=False, now=3000.0)
        self.assertEqual([], self._ledger())

    def test_unlabelled_fresh_oneshot_waits_for_its_role(self):
        s = self.state
        now = time.time()
        costs.collect(refresh_rate=False, now=now - 10)                     # empty baseline
        s.execute("insert into sessions values ('s_new','oneshot',NULL,?)", (now - 5,))
        _usage(s, "s_new", "claude-haiku-4-5", "", 1, 1, 10, 0, 100)
        costs.collect(refresh_rate=False, now=now)
        self.assertEqual([], self._ledger())            # held back: role not logged yet
        with patch.object(costs, "_pipeline_roles", return_value={"s_new": "pipeline:sweep/memory_keeper"}):
            costs.collect(refresh_rate=False, now=now + 60)
        self.assertEqual("pipeline:sweep/memory_keeper", self._ledger()[0][0])

    def test_report_totals_and_unpriced_models(self):
        s = self.state
        s.execute("insert into sessions values ('c','feishu',NULL,0)")
        costs.collect(refresh_rate=False, now=time.time() - 3600)
        _usage(s, "c", "claude-opus-5", "", 1, 0, 1000, 0, 0)
        _usage(s, "c", "gpt-5.6-sol", "", 1, 100, 10, 0, 0)
        s.execute("insert into messages values ('c','user',NULL,?)", (time.time(),))
        s.commit()
        costs.collect(refresh_rate=False)
        out = costs.report(days=14)
        self.assertIn("Total $0.03", out)
        s_ = costs.summary()
        self.assertEqual(1000 + 10, s_["tok_out"])
        self.assertAlmostEqual(0.025, s_["usd"], 6)              # 1000 out tokens on Opus = $0.025
        self.assertIn("1 messages from you", out)
        self.assertIn("no price for: gpt-5.6-sol", out)


if __name__ == "__main__":
    unittest.main()
