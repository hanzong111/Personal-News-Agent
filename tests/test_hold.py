"""Delivery hold: Hermes cron sends stdout the moment a job exits, so a job that starts
early delivers early. `hold.until_target()` pins the write to the target wall-clock time
— but must never wait on a catch-up run, where the slot is long past."""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PIPELINE_LOG_DIR", "/tmp/bursa-test-logs")

from pipeline import hold

MYT = timezone(timedelta(hours=8))
TARGETS = "09:00,14:00,21:00"


class HoldTest(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.addCleanup(os.environ.pop, "BURSA_DELIVER_AT", None)

    def _at(self, when: str, spec: str = TARGETS) -> float:
        os.environ["BURSA_DELIVER_AT"] = spec
        now = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MYT)
        return hold.until_target(now=now, sleep=self.slept.append)

    def test_holds_until_next_target(self):
        self.assertAlmostEqual(540, self._at("2026-09-24 08:51:00"), delta=1)
        self.assertEqual(1, len(self.slept))

    def test_holds_for_single_target_job(self):
        self.assertAlmostEqual(240, self._at("2026-09-24 18:26:00", "18:30"), delta=1)

    def test_no_hold_when_job_overran_its_target(self):
        self.assertEqual(0.0, self._at("2026-09-24 09:02:00"))
        self.assertEqual([], self.slept)

    def test_no_hold_on_catch_up_run(self):
        """Laptop woke at 15:34 for the missed 14:00 slot — emit now, never wait for 21:00."""
        self.assertEqual(0.0, self._at("2026-09-24 15:34:00"))
        self.assertEqual([], self.slept)

    def test_no_hold_when_next_target_is_tomorrow(self):
        self.assertEqual(0.0, self._at("2026-09-24 21:05:00"))

    def test_no_hold_without_target(self):
        """bursa-scan is a */30 poll with no target moment."""
        os.environ.pop("BURSA_DELIVER_AT", None)
        self.assertEqual(0.0, hold.until_target(sleep=self.slept.append))
        self.assertEqual([], self.slept)

    def test_ignores_malformed_target(self):
        self.assertAlmostEqual(540, self._at("2026-09-24 08:51:00", "nonsense,09:00"), delta=1)


if __name__ == "__main__":
    unittest.main()
