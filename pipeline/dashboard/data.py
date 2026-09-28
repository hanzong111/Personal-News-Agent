"""Read-only data access for the pipeline console. Pure functions over files that already exist:

  data/logs/events.jsonl            node events (see pipeline/log.py)
  ~/.hermes/cron/executions.db      Hermes run ledger
  ~/.hermes/cron/jobs.json          schedules, next run, model per job
  data/state/news.db                staged news memory (read-only sqlite URI)
  data/state/ops.json               watchdog findings

Nothing here writes anything.
"""
from __future__ import annotations
import json
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .. import config as pipeline_config

STATE = pipeline_config.STATE
NEWS_DB = pipeline_config.NEWS_DB
from ..log import EVENTS

HERMES = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
EXEC_DB = HERMES / "cron" / "executions.db"
JOBS = HERMES / "cron" / "jobs.json"
OPS_STATE = STATE / "ops.json"
MYT = timezone(timedelta(hours=8))

# cron script -> run-id prefix used by pipeline.log.new_run()
SCRIPT_KIND = {"bursa-scan.sh": "scan", "bursa-digest.sh": "digest", "bursa-weekly.sh": "weekly",
               "bursa-curate.sh": "curate", "malaysia-news.sh": "news", "bursa-ops.sh": "ops",
               "chat-sweep.sh": "sweep", "lss6-watch.sh": "lss6"}
TERMINAL = {"message", "silent"}
JOIN_WINDOW = 120  # seconds between hermes execution start and the run's first event


# ---------------------------------------------------------------- events
def read_events(max_lines: int = 200_000) -> list[dict]:
    if not EVENTS.exists():
        return []
    out = []
    for l in EVENTS.read_text(encoding="utf-8").splitlines()[-max_lines:]:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return out


class Tail:
    """Byte-offset tail of events.jsonl; survives rotation (file shrinks -> start over)."""

    def __init__(self):
        self.pos = EVENTS.stat().st_size if EVENTS.exists() else 0

    def poll(self) -> list[str]:
        if not EVENTS.exists():
            return []
        size = EVENTS.stat().st_size
        if size < self.pos:
            self.pos = 0
        if size == self.pos:
            return []
        with open(EVENTS, "rb") as f:
            f.seek(self.pos)
            chunk = f.read(size - self.pos)
        # only hand out complete lines
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return []
        self.pos += last_nl + 1
        return [l for l in chunk[: last_nl].decode("utf-8", "replace").split("\n") if l.strip()]


# ---------------------------------------------------------------- hermes ledger
def jobs() -> list[dict]:
    try:
        d = json.load(open(JOBS))
        jl = d["jobs"] if isinstance(d, dict) and "jobs" in d else d
        jl = list(jl.values()) if isinstance(jl, dict) else jl
    except Exception:
        return []
    out = []
    for j in jl:
        out.append({
            "id": j.get("id"), "name": j.get("name"), "script": j.get("script"),
            "kind": SCRIPT_KIND.get(j.get("script") or "", "other"),
            "schedule": (j.get("schedule") or {}).get("display") or j.get("schedule_display"),
            "next_run": j.get("next_run_at"), "last_run": j.get("last_run_at"),
            "last_status": j.get("last_status"), "last_error": j.get("last_error"),
            "deliver": j.get("deliver"), "model": j.get("model"), "no_agent": bool(j.get("no_agent")),
            "enabled": j.get("enabled", True), "paused": bool(j.get("paused_at")),
            "failure_streak": j.get("failure_streak") or 0,
        })
    return out


def executions(hours: float = 48) -> list[dict]:
    if not EXEC_DB.exists():
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        c = sqlite3.connect(f"file:{EXEC_DB}?mode=ro", uri=True)
        rows = c.execute("select id, job_id, started_at, finished_at, status, delivery_outcome, error "
                         "from executions where started_at >= ? order by started_at desc", (since,)).fetchall()
    except Exception:
        return []
    kinds = {j["id"]: j["kind"] for j in jobs()}
    names = {j["id"]: j["name"] for j in jobs()}
    out = []
    for xid, jid, st, fin, status, deliv, err in rows:
        t0 = _parse(st); t1 = _parse(fin)
        out.append({"id": xid, "job_id": jid, "job": names.get(jid, jid[:8]), "kind": kinds.get(jid, "other"),
                    "started": t0.timestamp() if t0 else None, "finished": t1.timestamp() if t1 else None,
                    "dur": (t1 - t0).total_seconds() if (t0 and t1) else None,
                    "status": status, "delivery": deliv, "error": err})
    return out


def _parse(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- runs
def summarise_run(rid: str, evs: list[dict]) -> dict:
    t0, t1 = evs[0]["ts"], evs[-1]["ts"]
    llm = [e for e in evs if e["node"] == "llm" and e["msg"] == "call done"]
    term = next((e for e in reversed(evs) if e["msg"] in TERMINAL or (e["node"] == "ops" and e["msg"] == "check")), None)
    errors = [e for e in evs if e["level"] == "ERROR"]
    nodes: list[str] = []
    for e in evs:
        if e["node"] not in nodes:
            nodes.append(e["node"])
    finished = term is not None or bool(errors and evs[-1]["level"] == "ERROR")
    gate = next((e for e in evs if e["msg"] == "gate"), None)
    return {
        "id": rid, "kind": rid.split("-")[0] if rid != "-" else "other",
        "start": t0, "end": t1, "dur": t1 - t0, "events": len(evs), "nodes": nodes,
        "llm_calls": len(llm),
        "tok_in": sum(int(e.get("tok_in") or 0) for e in llm),
        "tok_out": sum(int(e.get("tok_out") or 0) for e in llm),
        "usd": round(sum(float(e.get("usd") or 0) for e in llm), 4),
        "result": (("findings" if term.get("new") else "ok") if term and term["msg"] == "check"
                   else term["msg"] if term else ("error" if errors else "running")),
        "lines": term.get("lines") if term else None,
        "errors": len(errors), "finished": finished,
        "gate": {k: gate.get(k) for k in ("fetched", "new", "alerts", "queued_for_digest", "queued_for_judge")} if gate else None,
        "mode": next((e.get("mode") for e in evs if e["msg"] == "run start" and e.get("mode")), None),
        "hermes": None,
    }


def group_runs(events: list[dict]) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        runs[e.get("run") or "-"].append(e)
    return runs


def runs(n: int = 30, events: list[dict] | None = None) -> list[dict]:
    events = read_events() if events is None else events
    grouped = group_runs(events)
    out = [summarise_run(rid, evs) for rid, evs in grouped.items() if rid != "-"]
    out.sort(key=lambda r: r["start"], reverse=True)
    out = out[:n]
    _join_hermes(out)
    return out


def _join_hermes(run_list: list[dict]):
    if not run_list:
        return
    oldest = min(r["start"] for r in run_list)
    hours = max(1.0, (datetime.now(timezone.utc).timestamp() - oldest) / 3600 + 1)
    execs = executions(hours)
    for r in run_list:
        if r["mode"] not in (None, "cron"):     # manual runs (dry-run, preview, status…) never ran under hermes
            continue
        best = None
        for x in execs:
            if x["kind"] != r["kind"] or x["started"] is None:
                continue
            gap = abs(x["started"] - r["start"])
            if gap <= JOIN_WINDOW and (best is None or gap < best[0]):
                best = (gap, x)
        if best:
            x = best[1]
            r["hermes"] = {"execution": x["id"], "job": x["job"], "status": x["status"],
                           "delivery": x["delivery"], "dur": x["dur"], "error": x["error"],
                           "started": x["started"], "finished": x["finished"]}


def run_events(rid: str) -> list[dict]:
    return [e for e in read_events() if e.get("run") == rid]


# ---------------------------------------------------------------- overview / health / spend
def gateway_active() -> str:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway.service"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _count_lines(p: Path) -> int:
    try:
        return sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
    except OSError:
        return 0


def _queue_counts() -> dict[str, int]:
    """Read pending stages without opening the write-capable Memory API."""
    try:
        con = sqlite3.connect(f"file:{NEWS_DB}?mode=ro", uri=True)
        rows = dict(con.execute("SELECT stage,COUNT(*) FROM items WHERE stage IN ('digest','judge') GROUP BY stage"))
        con.close()
        return {"digest": int(rows.get("digest", 0)), "judge": int(rows.get("judge", 0))}
    except (OSError, sqlite3.Error):
        # Transitional fallback before the one-time migration.
        return {"digest": _count_lines(STATE / "digest_queue.jsonl"),
                "judge": _count_lines(STATE / "judge_queue.jsonl")}


STAGES = ("seen", "digest", "judge", "alert", "alerted", "digested", "dropped")   # lifecycle order
NOTE_PREVIEW = 220


def memory(days: int = 7) -> dict:
    """Snapshot of data/state/news.db for the MEMORY panel: rows by stage, size, retention health
    (rows the prune rules would delete right now), last prune, open stories and rolling notes.
    Read-only URI connection; never instantiates the write-capable Memory class."""
    from ..memory import _loads, prune_rules, prune_count_sql
    from .. import config as cfg
    empty = {"path": str(NEWS_DB), "exists": False, "bytes": 0, "items": 0, "by_stage": {s: 0 for s in STAGES},
             "keys": 0, "urls": 0, "stories": 0, "notes": 0, "oldest": None, "newest": None,
             "overdue": {}, "last_prune": None, "open_stories": [], "notes_list": [],
             "retention": {"raw": cfg.RAW_RETENTION_DAYS, "urls": cfg.URL_RETENTION_DAYS,
                           "delivered": cfg.DELIVERED_RETENTION_DAYS, "notes": cfg.NOTE_RETENTION_DAYS}}
    if not NEWS_DB.exists():
        return empty
    try:
        con = sqlite3.connect(f"file:{NEWS_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        return {**empty, "error": f"{type(e).__name__}: {e}"}
    try:
        one = lambda sql, args=(): con.execute(sql, args).fetchone()[0]
        stages = dict(con.execute("SELECT stage,COUNT(*) FROM items GROUP BY stage"))
        by_stage = {s: int(stages.get(s, 0)) for s in STAGES}
        by_stage.update({s: int(n) for s, n in stages.items() if s not in STAGES})   # anything unexpected still shows
        overdue = {name: int(one(prune_count_sql(sql), args)) for name, sql, args in prune_rules()}
        overdue["stories"] = int(one("SELECT COUNT(*) FROM stories WHERE NOT EXISTS (SELECT 1 FROM items WHERE items.story_id=stories.id)"))
        meta = dict(con.execute("SELECT key,value FROM meta"))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        stories = []
        for r in con.execute("SELECT s.*, i.stage AS root_stage FROM stories s LEFT JOIN items i ON i.id=s.root_id "
                             "WHERE s.last_seen>=? ORDER BY s.last_seen DESC LIMIT 40", (cutoff,)):
            d = dict(r); d["codes"] = _loads(d["codes"]); d["sectors"] = _loads(d["sectors"]); stories.append(d)
        notes = []
        for r in con.execute("SELECT key,kind,updated,text FROM notes ORDER BY kind,key"):
            d = dict(r); d["words"] = len(d["text"].split())
            d["preview"] = d["text"] if len(d["text"]) <= NOTE_PREVIEW else d["text"][:NOTE_PREVIEW].rsplit(" ", 1)[0] + " …"
            notes.append(d)
        # WAL journal counts toward what is on disk
        size = NEWS_DB.stat().st_size + sum(p.stat().st_size for p in (NEWS_DB.with_name(NEWS_DB.name + "-wal"),) if p.exists())
        return {
            **empty, "exists": True, "bytes": size, "items": sum(by_stage.values()), "by_stage": by_stage,
            "pending": sum(by_stage[s] for s in ("alert", "digest", "judge")),
            "keys": int(one("SELECT COUNT(*) FROM item_keys")), "urls": int(one("SELECT COUNT(*) FROM urls")),
            "stories": int(one("SELECT COUNT(*) FROM stories")), "notes": int(one("SELECT COUNT(*) FROM notes")),
            "oldest": one("SELECT MIN(fetched_at) FROM items"), "newest": one("SELECT MAX(fetched_at) FROM items"),
            "overdue": overdue, "overdue_total": sum(overdue.values()),
            "last_prune": _loads(meta.get("last_prune")) or None,
            "migration": _loads(meta.get("migration")) or None,
            "open_stories": stories, "notes_list": notes, "days": days, "names": _holding_names(),
        }
    except sqlite3.Error as e:
        return {**empty, "exists": True, "error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()


def _holding_names() -> dict[str, str]:
    """code -> short name, so the panel can say INARI instead of 0166. Empty on any config problem."""
    try:
        return {h.code: h.short for h in pipeline_config.load().holdings}
    except Exception:
        return {}


def overview() -> dict:
    findings = []
    try:
        st = json.loads(OPS_STATE.read_text())
        findings_since = st.get("since")
    except Exception:
        findings_since = None
    # last watchdog findings = WARN events from the ops node in the last 24h
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    for e in read_events(20_000):
        if e["node"] == "ops" and e["msg"] == "finding" and e["ts"] >= cutoff:
            findings.append({"ts": e["ts"], "text": e.get("text", "")})
    return {
        "now": datetime.now(timezone.utc).timestamp(),
        "gateway": gateway_active(),
        "jobs": jobs(),
        "queues": _queue_counts(),
        "watchdog": {"since": findings_since, "findings": findings[-10:]},
        "events_file_bytes": EVENTS.stat().st_size if EVENTS.exists() else 0,
    }


FETCH_MSGS = {"klse news": "klse", "bursa announcements": "bursa", "google news": "gnews",
              "prices": "prices", "firehose": "firehose", "feed": "news-feed"}


def health(hours: float = 24) -> dict:
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    events = [e for e in read_events() if e["ts"] >= cutoff]
    sources: dict[str, dict] = defaultdict(lambda: {"calls": 0, "items": 0, "errors": 0, "retries": 0})
    for e in events:
        if e["node"] in ("fetch", "news.fetch"):
            key = FETCH_MSGS.get(e["msg"])
            if key:
                s = sources[key]; s["calls"] += 1; s["items"] += int(e.get("items") or e.get("holdings") or 0)
                if e["level"] == "ERROR":
                    s["errors"] += 1
                if key == "prices":
                    s["errors"] += int(e.get("missing") or 0)
            elif e["msg"] == "http retry":
                sources["http"]["retries"] += 1
            elif e["msg"] == "feeds fetched":
                s = sources["news-feeds"]; s["calls"] += 1
                s["items"] += int(e.get("items") or 0)
                s["errors"] += int(e.get("feeds") or 0) - int(e.get("feeds_ok") or 0)
    run_list = [summarise_run(rid, evs) for rid, evs in group_runs(events).items() if rid != "-"]
    results = defaultdict(lambda: defaultdict(int))
    for r in run_list:
        results[r["kind"]][r["result"]] += 1
    execs = executions(hours)
    deliveries = defaultdict(int)
    overruns = []
    for x in execs:
        deliveries[x["delivery"] or x["status"]] += 1
        if x["dur"] and x["dur"] > 1800:
            overruns.append({"job": x["job"], "started": x["started"], "dur": x["dur"]})
    llm = [e for e in events if e["node"] == "llm" and e["msg"] == "call done"]
    return {
        "hours": hours,
        "sources": dict(sources),
        "runs": {k: dict(v) for k, v in results.items()},
        "deliveries": dict(deliveries),
        "overruns": overruns,
        "llm": {"calls": len(llm), "tok_in": sum(int(e.get("tok_in") or 0) for e in llm),
                "tok_out": sum(int(e.get("tok_out") or 0) for e in llm),
                "usd": round(sum(float(e.get("usd") or 0) for e in llm), 4),
                "errors": sum(1 for e in events if e["node"] == "llm" and e["level"] == "ERROR")},
    }


def spend(days: int = 7) -> dict:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    by_day: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "tok_in": 0, "tok_out": 0, "usd": 0.0}))
    by_role: dict[str, dict] = defaultdict(lambda: {"calls": 0, "tok_in": 0, "tok_out": 0, "usd": 0.0, "model": None})
    for e in read_events():
        if e["node"] != "llm" or e["msg"] != "call done" or e["ts"] < cutoff:
            continue
        day = datetime.fromtimestamp(e["ts"], MYT).strftime("%Y-%m-%d")
        role = e.get("role") or "?"
        for bucket in (by_day[day][role], by_role[role]):
            bucket["calls"] += 1
            bucket["tok_in"] += int(e.get("tok_in") or 0)
            bucket["tok_out"] += int(e.get("tok_out") or 0)
            bucket["usd"] = round(bucket["usd"] + float(e.get("usd") or 0), 4)
        by_role[role]["model"] = e.get("model_used") or e.get("model")
    return {"days": days, "by_day": {d: dict(v) for d, v in sorted(by_day.items())}, "by_role": dict(by_role)}


# ---------------------------------------------------------------- costs (Hermes usage ledger)
def costs() -> dict:
    """Whole-setup cost since the ledger started (costs.db): every Hermes call incl. cron agents + chat."""
    from .. import costs as ledger
    try:
        s = ledger.summary()
        if s.get("exists"):
            s["today"] = ledger.summary(since=datetime.now().strftime("%Y-%m-%d"))
            s["today"] = {k: s["today"].get(k) for k in ("usd", "tokens", "calls")}
        return s
    except Exception as e:
        return {"exists": False, "error": f"{type(e).__name__}: {e}"}
