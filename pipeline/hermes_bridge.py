"""Hermes-side half of the chat sweeper. Runs under Hermes' own interpreter
(~/.hermes/hermes-agent/venv/bin/python) so session ends and memory writes go through Hermes' APIs
(SessionDB.end_session bumps the conversation generation; MemoryStore locks, scans and enforces limits)
instead of raw SQL / file edits. Stdlib + Hermes only — never import `pipeline.*` here.

    routes                 -> JSON list of live gateway conversations (one per routing key)
    memory                 -> JSON of current MEMORY.md / USER.md entries + char limits
    apply  < request.json  -> JSON result; request = {"session_id", "expect_activity", "reason",
                                                      "memory": [ops], "user": [ops], "end_on_memory_fail"}
"""
from __future__ import annotations

import json
import os
import sys
import time

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
HERMES_CODE = os.path.join(HERMES_HOME, "hermes-agent")
sys.path.insert(0, HERMES_CODE)

from hermes_state import SessionDB  # noqa: E402


def _last_activity(conn, sid: str) -> float:
    row = conn.execute("select coalesce(last_activity_at, started_at), started_at from sessions where id=?",
                       (sid,)).fetchone()
    msg = conn.execute("select max(timestamp) from messages where session_id=?", (sid,)).fetchone()
    return max(x for x in (row[0] if row else 0, row[1] if row else 0, msg[0] if msg else 0) if x) if row else 0


def _live_lease(conn, sid: str) -> bool:
    return conn.execute("select 1 from session_turn_leases where conversation_id=? and expires_at>?",
                        (sid, time.time())).fetchone() is not None


def routes() -> list[dict]:
    db = SessionDB()
    out = []
    with db._read_ctx() as conn:
        rows = conn.execute("select session_key, entry_json from gateway_routing").fetchall()
    for key, entry_json in rows:
        entry = json.loads(entry_json or "{}")
        sid = entry.get("session_id")
        if not sid:
            continue
        tip = db.get_compression_tip(sid) or sid
        with db._read_ctx() as conn:
            row = conn.execute("select source, ended_at, end_reason, message_count from sessions where id=?",
                               (tip,)).fetchone()
            if row is None:
                continue
            out.append({"session_key": key, "session_id": tip, "platform": row[0], "ended": row[1] is not None,
                        "end_reason": row[2], "message_count": row[3] or 0,
                        "last_activity": _last_activity(conn, tip),
                        "busy": bool(entry.get("active_turn_token")) or _live_lease(conn, tip),
                        "suspended": bool(entry.get("suspended"))})
    return out


def _memory_ops(ops_by_target: dict) -> dict:
    from hermes_cli.config import load_config_readonly
    from tools.memory_tool_store import MemoryStore
    mem = (load_config_readonly() or {}).get("memory") or {}
    store = MemoryStore(memory_char_limit=int(mem.get("memory_char_limit", 2200)),
                        user_char_limit=int(mem.get("user_char_limit", 1375)))
    store.load_from_disk()
    results = {}
    for target, ops in ops_by_target.items():
        if ops:
            r = store.apply_batch(target, ops)
            results[target] = {"success": bool(r.get("success")), "error": r.get("error") or r.get("message", "")}
    return results


def apply(req: dict) -> dict:
    """Write memory, then end the session — but only if nobody touched it since it was judged idle."""
    db = SessionDB()
    sid = req["session_id"]
    with db._read_ctx() as conn:
        row = conn.execute("select ended_at from sessions where id=?", (sid,)).fetchone()
        if row is None or row[0] is not None:
            return {"ended": False, "skipped": "already ended or missing"}
        now_activity = _last_activity(conn, sid)
        if now_activity > float(req["expect_activity"]) + 1 or _live_lease(conn, sid):
            return {"ended": False, "skipped": "session became active"}
    memory = _memory_ops({"memory": req.get("memory") or [], "user": req.get("user") or []})
    if not req.get("end_on_memory_fail", True) and any(not r["success"] for r in memory.values()):
        return {"ended": False, "skipped": "memory rejected", "memory": memory}
    db.end_session(sid, req.get("reason") or "idle")
    return {"ended": True, "memory": memory}


def memory_snapshot() -> dict:
    from hermes_cli.config import load_config_readonly
    from tools.memory_tool_store import MemoryStore
    mem = (load_config_readonly() or {}).get("memory") or {}
    store = MemoryStore(memory_char_limit=int(mem.get("memory_char_limit", 2200)),
                        user_char_limit=int(mem.get("user_char_limit", 1375)))
    store.load_from_disk()
    return {"memory": store.memory_entries, "user": store.user_entries,
            "memory_limit": store.memory_char_limit, "user_limit": store.user_char_limit}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "routes":
        out = routes()
    elif cmd == "memory":
        out = memory_snapshot()
    elif cmd == "apply":
        out = apply(json.load(sys.stdin))
    else:
        sys.exit("usage: hermes_bridge.py routes|memory|apply")
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
