"""Pin delivery to a target wall-clock time.

Hermes cron delivers a job's stdout the moment the job exits — measured across 30 runs, the
delivery completes within ~1 s of the job's `finished_at`, before it is even stamped. There is
no queue and no target-time awareness. So a job that *starts* early also *delivers* early;
firing the cron sooner just slides the arrival window earlier instead of pinning it.

To land a message on a round time the job must wait before writing stdout:

    from pipeline import hold
    hold.until_target("digest")  # right before the stdout write; no-op when no target applies

Holding is opt-in per run: the cron wrapper sets BURSA_DELIVER_AT, so manual runs never wait.
  BURSA_DELIVER_AT=prefs          times come from data/preferences.yaml for the message passed to
                                  `until_target("digest" | "weekly" | "headlines")` (defaults if no file)
  BURSA_DELIVER_AT=18:30[,...]    explicit times; still overridden by preferences.yaml when it exists,
                                  so older wrappers follow what the user chose in setup
Nothing is held when:
  - BURSA_DELIVER_AT is unset (manual runs; bursa-scan, a */30 poll with no target moment),
  - the nearest upcoming target is further off than BURSA_MAX_HOLD_SEC — a catch-up run after
    the laptop slept, where the slot is long past and the next one is hours away, or
  - the job overran its own target, in which case the next one is too far and it emits now.

Silent ticks must not hold: there is no message to time.
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timedelta, timezone
from .log import get as _get_log

log = _get_log("hold")
MYT = timezone(timedelta(hours=8))
# Ceiling on how long a job may wait. Must exceed the largest cron lead (currently 10 min) and
# stay well under the gap between a job's targets, so a late run never waits for the next slot.
MAX_HOLD = float(os.environ.get("BURSA_MAX_HOLD_SEC", "900"))


def _targets(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = part.split(":")
            out.append((int(h), int(m)))
        except ValueError:
            log.warn("bad delivery target", value=part)
    return out


def _spec(message: str | None) -> str:
    env = os.environ.get("BURSA_DELIVER_AT", "").strip()
    if not env:
        return ""
    if message:
        from . import config, prefs
        if env == "prefs" or config.PREFS_FILE.exists():
            try:
                return prefs.deliver_at(prefs.load(), message) or ""
            except ValueError as e:
                log.warn("bad preferences, using wrapper times", error=str(e))
    return "" if env == "prefs" else env


def until_target(message: str | None = None, now: datetime | None = None, sleep=time.sleep) -> float:
    """Sleep until the next delivery target for `message` (digest | weekly | headlines).
    Returns seconds held (0.0 if not held)."""
    spec = _spec(message)
    if not spec:
        return 0.0
    now = now or datetime.now(MYT)
    best = None
    for h, m in _targets(spec):
        for day in (0, 1):                      # today's slot, else tomorrow's first
            t = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now and (best is None or t < best):
                best = t
    if best is None:
        return 0.0
    wait = (best - now).total_seconds()
    if wait > MAX_HOLD:
        log.info("not holding", target=best.strftime("%H:%M"), wait=round(wait),
                 reason="next target too far — late or catch-up run")
        return 0.0
    log.info("holding for delivery", target=best.strftime("%H:%M"), wait=round(wait, 1))
    sleep(wait)
    return wait
