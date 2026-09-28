"""Cost ledger: every model call Hermes makes, attributed to a job / pipeline role / chat, with token counts.

Source of truth is Hermes' own per-session, per-model usage counters (`~/.hermes/state.db`,
`session_model_usage`: calls, input, output, cache-read, cache-write — the numbers the API returned). That
covers what `events.jsonl` can't see: cron agent turns (digest, weekly), chat, and Hermes' side calls
(compression summaries, title generation, background memory review). `collect` snapshots those cumulative
counters and stores only the growth since the last snapshot, so usage lands on the day it happened and
survives Hermes pruning old sessions. Dollars are API-equivalent (list prices, 5-min cache writes): under
Claude Code OAuth nothing is billed per token, but this is what the setup would cost on the API.

    python -m pipeline.costs collect            # cron, every 10 min, silent
    python -m pipeline.costs report [--days 14] [--since YYYY-MM-DD] [--by-day]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from agents.llm import PRICE
from pipeline import config
from pipeline.log import EVENTS


HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
STATE_DB = os.path.join(HERMES_HOME, "state.db")
JOBS = os.path.join(HERMES_HOME, "cron/jobs.json")
LEDGER = config.STATE / "costs.db"
CHAT = {"feishu", "telegram", "weixin", "discord", "slack", "whatsapp", "signal"}
PENDING_ONESHOT_S = 600

SCHEMA = """
create table if not exists snap (session_id text, model text, task text, calls int, inp int, out int, cr int, cw int,
                                 primary key (session_id, model, task));
create table if not exists usage (ts real, day text, session_id text, category text, model text, task text,
                                  calls int, inp int, out int, cr int, cw int, usd real);
create index if not exists usage_day on usage(day);
create table if not exists meta (key text primary key, value text);
"""


def usd(model: str, inp: int, out: int, cr: int, cw: int) -> float | None:
    p = PRICE.get(model)
    return None if p is None else (inp * p[0] + out * p[1] + cr * p[2] + cw * p[3]) / 1e6


def _job_names() -> dict:
    try:
        d = json.load(open(JOBS))
        return {j["id"]: j.get("name") or j["id"] for j in (d["jobs"] if isinstance(d, dict) and "jobs" in d else d)}
    except (OSError, ValueError, KeyError):
        return {}


def _pipeline_roles() -> dict:
    """Hermes one-shot session id -> 'pipeline:<run kind>/<role>', from the llm 'call done' events."""
    out = {}
    for path in (EVENTS.with_suffix(EVENTS.suffix + ".1"), EVENTS):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if '"call done"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("session"):
                out[e["session"]] = f"pipeline:{(e.get('run') or '?').split('-')[0]}/{e.get('role') or '?'}"
    return out


def categorize(sid: str, source: str, parent_source: str | None, jobs: dict, roles: dict) -> str:
    if sid in roles:
        return roles[sid]
    if source == "cron":
        parts = sid.split("_")
        return f"cron:{jobs.get(parts[1], parts[1]) if len(parts) > 1 else '?'}"
    if source in CHAT:
        return f"chat:{source}"
    if source == "subagent" and parent_source:
        return f"chat:{parent_source}" if parent_source in CHAT else f"subagent:{parent_source}"
    return f"other:{source or '?'}"


def collect(now: float | None = None, refresh_rate: bool = True) -> int:
    now = now or time.time()
    src = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        # Hermes keeps several rows per (session, model, task) — one per billing route — so sum them.
        rows = src.execute(
            "select u.session_id, u.model, coalesce(u.task,''), sum(u.api_call_count), sum(u.input_tokens), "
            "sum(u.output_tokens), sum(u.cache_read_tokens), sum(u.cache_write_tokens), s.source, p.source, s.started_at "
            "from session_model_usage u left join sessions s on s.id = u.session_id "
            "left join sessions p on p.id = s.parent_session_id "
            "group by u.session_id, u.model, coalesce(u.task,'')").fetchall()
    finally:
        src.close()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(LEDGER)
    db.executescript(SCHEMA)
    first = db.execute("select value from meta where key='start'").fetchone() is None
    snap = {(r[0], r[1], r[2]): r[3:] for r in db.execute("select * from snap")}
    jobs = roles = None
    added = 0
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    for sid, model, task, *counts, source, parent_source, started in rows:
        counts = [int(x or 0) for x in counts]
        key = (sid, model or "?", task)
        prev = snap.get(key, (0, 0, 0, 0, 0))
        delta = [c - p if c >= p else c for c, p in zip(counts, prev)]   # counter went down = row was reset
        if tuple(counts) == tuple(prev):
            continue
        if not first:
            if roles is None:
                jobs, roles = _job_names(), _pipeline_roles()
            if source == "oneshot" and sid not in roles and now - (started or 0) < PENDING_ONESHOT_S:
                continue   # pipeline call may not have logged its role yet: pick it up next run
        db.execute("insert or replace into snap values (?,?,?,?,?,?,?,?)", (*key, *counts))
        if first or not any(delta):
            continue
        cat = categorize(sid, source, parent_source, jobs, roles)
        db.execute("insert into usage values (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now, day, sid, cat, key[1], task, *delta, usd(key[1], *delta[1:])))
        added += 1
    if first:
        db.execute("insert into meta values ('start', ?)", (datetime.fromtimestamp(now).isoformat(timespec="seconds"),))
    if refresh_rate:
        _refresh_rate(db, now)
    db.commit()
    db.close()
    return added


def _chat_user_messages(since_ts: float) -> int:
    src = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        q = ",".join("?" * len(CHAT))
        return src.execute(f"select count(*) from messages m join sessions s on s.id = m.session_id "
                           f"where s.source in ({q}) and m.role='user' and m.display_kind is null "
                           f"and m.timestamp >= ?", (*CHAT, since_ts)).fetchone()[0]
    finally:
        src.close()


def _fmt_tok(n: float) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _refresh_rate(db: sqlite3.Connection, now: float) -> None:
    """USD→MYR from Yahoo (MYR=X), at most once a day; keeps the last good rate on failure."""
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    got = db.execute("select value from meta where key='usdmyr_day'").fetchone()
    if got and got[0] == day:
        return
    try:
        from pipeline.fetchers import history   # lazy: curl_cffi only needed here
        rows = history("MYR=X", "5d")
        rate = float(rows[-1]["close"]) if rows else None
    except Exception:
        rate = None
    if rate and 2 < rate < 10:   # sanity band for USD/MYR
        db.execute("insert or replace into meta values ('usdmyr', ?)", (f"{rate:.4f}",))
        db.execute("insert or replace into meta values ('usdmyr_day', ?)", (day,))


def summary(days: int | None = None, since: str | None = None) -> dict:
    """Aggregates for the report and the console. days=None and since=None -> everything since ledger start."""
    if not LEDGER.exists():
        return {"exists": False}
    db = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
    try:
        meta = dict(db.execute("select key, value from meta").fetchall())
        lo = since or ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days else "0000")
        rows = db.execute("select day, category, model, task, calls, inp, out, cr, cw, usd from usage where day >= ?",
                          (lo,)).fetchall()
    finally:
        db.close()
    start = datetime.fromisoformat(meta["start"]) if meta.get("start") else datetime.now()
    first = max(start, datetime.fromisoformat(lo)) if lo != "0000" else start
    span = max((datetime.now() - first).total_seconds() / 86400, 1 / 24)
    cat = defaultdict(lambda: {"calls": 0, "tok_in": 0, "cached": 0, "tok_out": 0, "usd": 0.0})
    mdl = defaultdict(lambda: {"calls": 0, "usd": 0.0})
    daily = defaultdict(lambda: {"usd": 0.0, "tokens": 0})
    unpriced = set()
    for day, c, m, task, calls, inp, out, cr, cw, u in rows:
        b = cat[f"{c} [{task}]" if task else c]
        b["calls"] += calls; b["tok_in"] += inp + cr + cw; b["cached"] += cr; b["tok_out"] += out; b["usd"] += u or 0
        mdl[m]["calls"] += calls; mdl[m]["usd"] += u or 0
        daily[day]["usd"] += u or 0; daily[day]["tokens"] += inp + cr + cw + out
        if u is None:
            unpriced.add(m)
    total = sum(v["usd"] for v in cat.values())
    tok_in = sum(v["tok_in"] for v in cat.values())
    tok_out = sum(v["tok_out"] for v in cat.values())
    chat_usd = sum(v["usd"] for k, v in cat.items() if k.startswith("chat:"))
    try:
        msgs = _chat_user_messages(first.timestamp())
    except sqlite3.Error:
        msgs = 0
    rate = float(meta["usdmyr"]) if meta.get("usdmyr") else None
    return {"exists": True, "start": start.isoformat(timespec="seconds"), "from": first.isoformat(timespec="seconds"),
            "span_days": span, "usd": total, "myr": total * rate if rate else None,
            "usdmyr": rate, "usdmyr_day": meta.get("usdmyr_day"),
            "tok_in": tok_in, "tok_out": tok_out, "tokens": tok_in + tok_out,
            "cached": sum(v["cached"] for v in cat.values()), "calls": sum(v["calls"] for v in cat.values()),
            "per_day_usd": total / span, "month_usd": total / span * 30,
            "month_myr": total / span * 30 * rate if rate else None,
            "chat": {"messages": msgs, "usd": chat_usd, "per_message_usd": chat_usd / msgs if msgs else None},
            "by_category": [{"category": k, **v} for k, v in sorted(cat.items(), key=lambda kv: -kv[1]["usd"])],
            "by_model": [{"model": k, **v} for k, v in sorted(mdl.items(), key=lambda kv: -kv[1]["usd"])],
            "by_day": [{"day": d, **v} for d, v in sorted(daily.items())], "unpriced": sorted(unpriced)}


def report(days: int | None = 14, since: str | None = None, by_day: bool = False) -> str:
    s = summary(days, since)
    if not s.get("exists") or not s["calls"]:
        return f"No usage recorded yet (ledger start: {s.get('start', 'never')})."
    rm = lambda usd: f" (≈RM{usd * s['usdmyr']:.2f})" if s["usdmyr"] else ""
    lines = [f"💰 **Running cost** — {datetime.fromisoformat(s['from']):%d %b} → {datetime.now():%d %b %H:%M} "
             f"({s['span_days']:.1f} days)",
             "API-equivalent at list prices (OAuth = no per-token bill).", "",
             f"**Total ${s['usd']:.2f}**{rm(s['usd'])} · {_fmt_tok(s['tok_in'])} in / {_fmt_tok(s['tok_out'])} out · "
             f"~${s['month_usd']:.0f}/month{rm(s['month_usd'])} at this rate", "", "**By job**"]
    for v in s["by_category"]:
        lines.append(f"• {v['category']}: ${v['usd']:.2f} · {v['calls']} calls · {_fmt_tok(v['tok_in'])} in "
                     f"({_fmt_tok(v['cached'])} cached) / {_fmt_tok(v['tok_out'])} out")
    lines += ["", "**By model**"] + [f"• {v['model']}: ${v['usd']:.2f} · {v['calls']} calls" for v in s["by_model"]]
    if s["chat"]["messages"]:
        lines += ["", f"**Chat**: {s['chat']['messages']} messages from you → "
                      f"${s['chat']['per_message_usd']:.3f} per message"]
    if by_day:
        lines += ["", "**By day**"] + [f"• {d['day']}: ${d['usd']:.2f}" for d in s["by_day"]]
    if s["unpriced"]:
        lines += ["", f"⚠️ no price for: {', '.join(s['unpriced'])} (counted as $0)"]
    if s["usdmyr"]:
        lines += ["", f"RM at {s['usdmyr']:.3f} MYR/USD (Yahoo, {s['usdmyr_day']})"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=14)
    r.add_argument("--since")
    r.add_argument("--by-day", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        collect()   # no log run: 144/day would flood the console; a crash fails the cron job → bursa-ops alerts
    else:
        print(report(a.days, a.since, a.by_day))


if __name__ == "__main__":
    main()
