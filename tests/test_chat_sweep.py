from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from pipeline import chat_sweep


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).astimezone().timestamp()


class DueTest(unittest.TestCase):
    def test_idle_after_threshold(self):
        now = _ts("2026-09-23T15:00")
        self.assertEqual("idle", chat_sweep.due(now - 4 * 3600, now, 4, None))
        self.assertIsNone(chat_sweep.due(now - 3.9 * 3600, now, 4, None))

    def test_daily_boundary_needs_quiet_and_crossing(self):
        # active 03:00, now 04:10 -> crossed 04:00 and quiet 70 min
        self.assertEqual("daily", chat_sweep.due(_ts("2026-09-23T03:00"), _ts("2026-09-23T04:10"), 4, "04:00"))
        # active 03:50, now 04:10 -> crossed but only 20 min quiet: keep
        self.assertIsNone(chat_sweep.due(_ts("2026-09-23T03:50"), _ts("2026-09-23T04:10"), 4, "04:00"))
        # active 05:00, now 07:00 -> no boundary crossed since
        self.assertIsNone(chat_sweep.due(_ts("2026-09-23T05:00"), _ts("2026-09-23T07:00"), 4, "04:00"))
        # before today's boundary: yesterday's 04:00 counts
        self.assertIsNone(chat_sweep.due(_ts("2026-09-23T01:00"), _ts("2026-09-23T03:00"), 4, "04:00"))
        self.assertEqual("daily", chat_sweep.due(_ts("2026-09-23T03:30"), _ts("2026-09-24T02:00"), 48, "04:00"))


ROUTE = {"session_key": "k", "session_id": "s1", "platform": "feishu", "last_activity": 100.0}
SNAP = {"memory": [], "user": [], "memory_limit": 2200, "user_limit": 1375}


class DistillTest(unittest.TestCase):
    def test_no_user_messages_ends_without_model(self):
        calls = []
        with patch.object(chat_sweep, "transcript", return_value=("[assistant] alert", 1)), \
             patch.object(chat_sweep, "_bridge", side_effect=lambda c, p=None: calls.append((c, p)) or {"ended": True}), \
             patch("agents.memory_keeper.run") as keeper:
            res = chat_sweep._distill_and_end(ROUTE, "idle", use_llm=True)
        keeper.assert_not_called()
        self.assertTrue(res["ended"])
        self.assertEqual(["apply"], [c for c, _ in calls])
        self.assertEqual(100.0, calls[0][1]["expect_activity"])

    def test_rejected_store_is_retried_once_then_ended(self):
        applies = []

        def bridge(cmd, payload=None):
            if cmd == "memory":
                return SNAP
            applies.append(payload)
            if len(applies) == 1:
                return {"ended": False, "skipped": "memory rejected",
                        "memory": {"memory": {"success": True, "error": ""},
                                   "user": {"success": False, "error": "over limit"}}}
            return {"ended": True, "memory": {"user": {"success": True, "error": ""}}}

        first = {"memory": [{"action": "add", "content": "a"}], "user": [{"action": "add", "content": "b"}]}
        second = {"memory": [{"action": "add", "content": "x"}], "user": [{"action": "add", "content": "c"}]}
        with patch.object(chat_sweep, "transcript", return_value=("[user] hi", 1)), \
             patch.object(chat_sweep, "_bridge", side_effect=bridge), \
             patch("agents.memory_keeper.run", side_effect=[first, second]) as keeper:
            res = chat_sweep._distill_and_end(ROUTE, "idle", use_llm=True)
        self.assertEqual(2, keeper.call_count)
        self.assertIn("over limit", keeper.call_args.kwargs["feedback"])
        self.assertFalse(applies[0]["end_on_memory_fail"])
        self.assertEqual([], applies[1]["memory"])   # the store that succeeded is not re-sent
        self.assertEqual(second["user"], applies[1]["user"])
        self.assertTrue(res["ended"])
        self.assertEqual({"memory", "user"}, set(res["memory"]))


if __name__ == "__main__":
    unittest.main()
