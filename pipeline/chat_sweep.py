"""Chat sweeper: auto-`/new` for idle Hermes chat sessions (Feishu / Telegram / Weixin …).

Hermes v0.21 gateway sessions never reset on their own, so one chat grows forever (compressed, but every
turn still carries the big prefix). Every 30 min this finds live chats that went quiet, lets the memory
keeper (Haiku) save lasting facts into MEMORY.md / USER.md, then ends the session with a reset reason
Hermes already honours ('idle' / 'daily'). The gateway sees the ended row on the next message and starts
a fresh session; the old transcript stays searchable via session_search.

    python -m pipeline.chat_sweep [--dry-run] [--no-llm] [--idle-hours 4] [--daily-at 04:00]

Stdout stays empty (the cron job is silent); everything goes to the pipeline log (node "sweep").
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.log import get as _get_log, new_run

log = _get_log("sweep")

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
HERMES_PY = os.path.join(HERMES_HOME, "hermes-agent/venv/bin/python")
BRIDGE = str(Path(__file__).resolve().parent / "hermes_bridge.py")
STATE_DB = os.path.join(HERMES_HOME, "state.db")
IDLE_HOURS = 4.0
DAILY_AT = "04:00"
DAILY_MIN_QUIET = 30 * 60      # the daily boundary never cuts a chat that was active in the last 30 min
MSG_CHARS = 1500               # per message in the transcript fed to the memory keeper
TRANSCRIPT_CHARS = 60_000      # tail of the conversation (~15k tokens)


def due(last_activity: float, now: float, idle_hours: float = IDLE_HOURS, daily_at: str | None = DAILY_AT) -> str | None:
    """Reset reason for a chat last active at *last_activity*, or None to leave it alone."""
    quiet = now - last_activity
    if quiet >= idle_hours * 3600:
        return "idle"
    if daily_at and quiet >= DAILY_MIN_QUIET:
        hh, mm = (int(x) for x in daily_at.split(":"))
        local = datetime.fromtimestamp(now).astimezone()
        boundary = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if boundary > local:
            boundary -= timedelta(days=1)
        if last_activity < boundary.timestamp():
            return "daily"
    return None


def _bridge(cmd: str, payload: dict | None = None) -> dict | list:
    r = subprocess.run([HERMES_PY, BRIDGE, cmd], input=json.dumps(payload) if payload is not None else None,
                       capture_output=True, text=True, timeout=120, cwd="/tmp")
    if r.returncode != 0:
        raise RuntimeError(f"bridge {cmd} failed ({r.returncode}): {r.stderr[-400:]}")
    return json.loads(r.stdout)


def transcript(session_id: str) -> tuple[str, int]:
    """User + assistant text of the live (un-compacted) part of a session, newest tail kept."""
    c = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        rows = c.execute("select role, content from messages where session_id=? and active=1 "
                         "and role in ('user','assistant') and display_kind is null "
                         "and content is not null and content != '' order by id", (session_id,)).fetchall()
    finally:
        c.close()
    lines = []
    for role, content in rows:
        text = content if len(content) <= MSG_CHARS else content[:MSG_CHARS] + " …[cut]"
        lines.append(f"[{role}] {text}")
    body = "\n\n".join(lines)
    if len(body) > TRANSCRIPT_CHARS:
        body = "…[earlier messages cut]\n\n" + body[-TRANSCRIPT_CHARS:]
    return body, len(rows)


def _distill_and_end(route: dict, reason: str, use_llm: bool) -> dict:
    from agents import memory_keeper
    sid = route["session_id"]
    req = {"session_id": sid, "expect_activity": route["last_activity"], "reason": reason, "memory": [], "user": []}
    text, n = transcript(sid)
    if not (use_llm and n and "[user]" in text):
        return _bridge("apply", req)
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot = _bridge("memory")
    ops = memory_keeper.run(text, n, route["platform"], snapshot, today)
    if not ops["memory"] and not ops["user"]:
        return _bridge("apply", req)
    res = _bridge("apply", {**req, **ops, "end_on_memory_fail": False})
    if res.get("skipped") != "memory rejected":
        return res
    # One retry for the stores Hermes rejected (over limit / unmatched old_text); then end regardless.
    failed = {t: r["error"] for t, r in res["memory"].items() if not r["success"]}
    log.warn("memory ops rejected", session=sid, errors=json.dumps(failed, ensure_ascii=False)[:300])
    retry = memory_keeper.run(text, n, route["platform"], _bridge("memory"), today,
                              feedback="\n".join(f"{t}: {e}" for t, e in failed.items()))
    res2 = _bridge("apply", {**req, **{t: retry[t] for t in failed}})
    res2["memory"] = {**{t: r for t, r in res["memory"].items() if r["success"]}, **(res2.get("memory") or {})}
    return res2


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would be reset; no model, no writes")
    ap.add_argument("--no-llm", action="store_true", help="end idle sessions without the memory pass")
    ap.add_argument("--idle-hours", type=float, default=IDLE_HOURS)
    ap.add_argument("--daily-at", default=DAILY_AT, help="HH:MM local daily boundary; 'off' disables")
    a = ap.parse_args(argv)
    new_run("sweep")
    daily_at = None if a.daily_at == "off" else a.daily_at
    log.info("run start", mode="dry-run" if a.dry_run else "cron", idle_hours=a.idle_hours, daily_at=daily_at or "off")

    now = time.time()
    with log.span("routes"):
        routes = _bridge("routes")
    reset = kept = 0
    for route in routes:
        key, sid = route["session_key"], route["session_id"]
        if route["ended"] or route["suspended"] or not route["message_count"]:
            continue
        reason = due(route["last_activity"], now, a.idle_hours, daily_at)
        quiet_h = round((now - route["last_activity"]) / 3600, 1)
        if not reason or route["busy"]:
            kept += 1
            log.debug("keep", session=sid, platform=route["platform"], quiet_h=quiet_h, busy=route["busy"])
            continue
        if a.dry_run:
            log.info("would reset", session=sid, platform=route["platform"], reason=reason, quiet_h=quiet_h,
                     messages=route["message_count"])
            reset += 1
            continue
        try:
            with log.span("reset", session=sid, platform=route["platform"], reason=reason, quiet_h=quiet_h):
                res = _distill_and_end(route, reason, use_llm=not a.no_llm)
        except Exception as e:  # one broken chat must not block the others; retried next run
            log.error("reset failed", session=sid, key=key, err=f"{type(e).__name__}: {e}"[:300])
            continue
        mem = res.get("memory") or {}
        log.info("ended" if res.get("ended") else "skipped", session=sid, platform=route["platform"], reason=reason,
                 why=res.get("skipped", ""), memory=",".join(f"{t}:{'ok' if r['success'] else 'rejected'}"
                                                             for t, r in mem.items()) or "none")
        reset += bool(res.get("ended"))
    log.info("silent", sessions=len(routes), reset=reset, kept=kept)


if __name__ == "__main__":
    main()
