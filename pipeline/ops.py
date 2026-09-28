"""OPS WATCHDOG — no LLM. Run by Hermes cron (--no-agent) every 30 min.

Checks, and prints an alert only when something is wrong (empty stdout = silent):
  • cron executions since the last check: failed / not delivered / overran (> OVERRUN_FACTOR × interval)
  • pipeline events since the last check: ERROR lines, feeds_ok below threshold, briefer/judge/classifier failures
  • gateway service not active
Each finding is reported once (state in data/state/ops.json).
"""
from __future__ import annotations
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .config import STATE
from .log import get as _get_log, new_run, EVENTS

log = _get_log("ops")
OPS_STATE = STATE / "ops.json"
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
EXEC_DB = os.path.join(HERMES_HOME, "cron/executions.db")
JOBS = os.path.join(HERMES_HOME, "cron/jobs.json")
OVERRUN_FACTOR = 2.0
MIN_FEEDS_OK = 10           # of 13 Malaysia feeds
MYT = timezone(timedelta(hours=8))


def _load_state() -> dict:
    try:
        return json.loads(OPS_STATE.read_text())
    except Exception:
        return {"since": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "seen": []}


def _jobs() -> dict:
    try:
        d = json.load(open(JOBS))
        jl = d["jobs"] if isinstance(d, dict) and "jobs" in d else d
        return {(j.get("id") or j.get("job_id")): j for j in (jl.values() if isinstance(jl, dict) else jl)}
    except Exception:
        return {}


def _interval_seconds(job: dict) -> int | None:
    expr = (job.get("schedule") or {}).get("expr", "")
    minute = expr.split()[0] if expr else ""
    if minute.startswith("*/"):
        return int(minute[2:]) * 60
    return 24 * 3600 if expr else None


def check_executions(since: datetime, jobs: dict) -> list[str]:
    findings = []
    try:
        c = sqlite3.connect(f"file:{EXEC_DB}?mode=ro", uri=True)
        rows = c.execute("select id, job_id, started_at, finished_at, status, delivery_outcome, error from executions "
                         "where started_at >= ? order by started_at", (since.isoformat(),)).fetchall()
    except Exception as e:
        return [f"⚠️ cannot read cron ledger: {e}"]
    for xid, jid, st, fin, status, deliv, err in rows:
        name = jobs.get(jid, {}).get("name", jid[:8])
        t_st = datetime.fromisoformat(st)
        t_fin = datetime.fromisoformat(fin) if fin else None
        dur = (t_fin - t_st).total_seconds() if t_fin else (datetime.now(timezone.utc) - t_st).total_seconds()
        key = f"exec:{xid}"
        if status not in ("completed",) and t_fin:
            findings.append((key, f"🔴 {name} {t_st.astimezone(MYT):%H:%M} status={status} {('— ' + err[:120]) if err else ''}"))
        elif deliv not in ("delivered", "suppressed", None):
            findings.append((key, f"🟠 {name} {t_st.astimezone(MYT):%H:%M} delivery={deliv} {('— ' + err[:120]) if err else ''}"))
        iv = _interval_seconds(jobs.get(jid, {}))
        if iv and dur > OVERRUN_FACTOR * iv and dur > 600:
            findings.append((key + ":overrun", f"🟠 {name} {t_st.astimezone(MYT):%H:%M} took {dur/60:.0f} min "
                                               f"(interval {iv//60} min) — machine asleep or job hung"))
        if not t_fin and dur > 1800:
            findings.append((key + ":stuck", f"🔴 {name} started {t_st.astimezone(MYT):%H:%M} still running after {dur/60:.0f} min"))
    return findings


def check_events(since: datetime) -> list[str]:
    findings = []
    if not EVENTS.exists():
        return findings
    for l in EVENTS.read_text(encoding="utf-8").splitlines()[-5000:]:
        try:
            e = json.loads(l)
        except json.JSONDecodeError:
            continue
        if e["ts"] < since.timestamp():
            continue
        t = datetime.fromtimestamp(e["ts"], MYT).strftime("%H:%M")
        if e["level"] == "ERROR":
            findings.append((f"ev:{e['ts']}", f"🔴 {t} [{e['node']}] {e['msg']} {e.get('err', '')[:120]}"))
        elif e["node"] == "news.fetch" and e["msg"] == "feeds fetched" and int(e.get("feeds_ok", 99)) < MIN_FEEDS_OK:
            findings.append((f"ev:{e['ts']}", f"🟠 {t} news feeds degraded: {e['feeds_ok']}/{e['feeds']} ok"))
        elif e["node"] == "fetch" and e["msg"] == "prices" and int(e.get("missing", 0)) > 0:
            findings.append((f"ev:{e['ts']}", f"🟠 {t} price fetch missing {e['missing']} holding(s)"))
        elif e["node"] == "agent.classifier" and e["msg"].startswith("stories left unlabelled"):
            findings.append((f"ev:{e['ts']}", f"🟠 {t} classifier left {e.get('count')} stories unlabelled"))
    return findings


def check_gateway() -> list[str]:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway.service"], capture_output=True, text=True, timeout=10)
        if r.stdout.strip() != "active":
            return [("gw", f"🔴 hermes-gateway.service is {r.stdout.strip() or 'unknown'}")]
    except Exception as e:
        return [("gw", f"⚠️ cannot query gateway: {e}")]
    return []


def main(argv=None):
    new_run("ops")
    state = _load_state()
    since = datetime.fromisoformat(state["since"])
    seen = set(state.get("seen", []))
    jobs = _jobs()
    found = check_executions(since - timedelta(hours=3), jobs) + check_events(since) + check_gateway()
    fresh = [(k, msg) for k, msg in found if k not in seen]
    log.info("check", since=since.astimezone(MYT).strftime("%H:%M"), findings=len(found), new=len(fresh), jobs=len(jobs))
    now = datetime.now(timezone.utc)
    OPS_STATE.write_text(json.dumps({"since": now.isoformat(), "seen": sorted((seen | {k for k, _ in found}))[-500:]}))
    if not fresh:
        return
    out = [f"🛠️ **Pipeline watchdog** — {now.astimezone(MYT):%a %H:%M}", ""] + [m for _, m in fresh]
    out += ["", "Logs: `python -m pipeline.log runs` · `python -m pipeline.log tail --level WARN`"]
    for _, m in fresh:
        log.warn("finding", text=m)
    print("\n".join(out))


if __name__ == "__main__":
    main()
