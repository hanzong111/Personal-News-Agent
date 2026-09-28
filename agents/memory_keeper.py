"""MEMORY KEEPER — one Haiku pass over an idle chat before the sweeper ends it: which lasting facts
belong in Hermes' built-in MEMORY.md / USER.md. Returns batch ops; Hermes' MemoryStore validates them."""
from __future__ import annotations

from . import llm
from pipeline.log import get as _get_log

log = _get_log("agent.memory_keeper")
DELIM = "\n§\n"

PROMPT = """You maintain the long-term memory of a personal assistant ("Hermes") that the user talks to over
chat apps. The conversation below is about to be closed; only what you save survives into future chats.
Return one JSON object only:
  {{"memory": [ops], "user": [ops]}}
where each op is {{"action":"add","content":"..."}} or {{"action":"replace","old_text":"...","content":"..."}}
or {{"action":"remove","old_text":"..."}}. "old_text" must be a unique substring of an existing entry.

- "user" = facts about the user: identity, preferences, standing instructions, how they like replies.
- "memory" = facts about their environment, projects, decisions and ongoing work worth recalling later.
Save only durable, reusable facts that are NOT already in memory. Prefer "replace" to update or merge an
existing entry over adding a near-duplicate. Skip small talk, one-off questions, debugging steps,
news/alert content and anything a tool can re-read later. Skip code internals (file paths, table names,
function names, benchmarks): the project keeps its own docs and they go stale in memory.
Never save secrets, tokens or passwords.
Each entry: one or two plain sentences (max ~250 chars), third person, absolute dates (today is {today}).
After your ops, each store must stay within its limit — remove or shorten stale entries if needed.
Most conversations add nothing: then return {{"memory": [], "user": []}}.

CURRENT MEMORY ({mem_used}/{mem_limit} chars):
{memory}

CURRENT USER PROFILE ({user_used}/{user_limit} chars):
{user}

CONVERSATION ({platform}, {n} messages, oldest first; may be truncated at the start):
{transcript}
"""


def _entries(entries: list[str]) -> str:
    return "\n".join(f"- {e}" for e in entries) or "(empty)"


def _ops(raw) -> list[dict]:
    ops = []
    for op in raw if isinstance(raw, list) else []:
        if not isinstance(op, dict) or op.get("action") not in {"add", "replace", "remove"}:
            continue
        ops.append({k: str(op[k]).strip() for k in ("action", "content", "old_text") if op.get(k)})
    return ops


def run(transcript: str, n: int, platform: str, snapshot: dict, today: str, feedback: str = "") -> dict:
    mem, user = snapshot["memory"], snapshot["user"]
    prompt = PROMPT.format(today=today, platform=platform, n=n, transcript=transcript,
                           memory=_entries(mem), mem_used=len(DELIM.join(mem)), mem_limit=snapshot["memory_limit"],
                           user=_entries(user), user_used=len(DELIM.join(user)), user_limit=snapshot["user_limit"])
    if feedback:
        prompt += f"\nYOUR PREVIOUS OPS WERE REJECTED — fix and resend all ops:\n{feedback}\n"
    log.info("start", platform=platform, messages=n, chars=len(transcript), retry=bool(feedback))
    out = llm.call_json(prompt, model=llm.HAIKU, effort="low", role="memory_keeper")
    out = out if isinstance(out, dict) else {}
    ops = {"memory": _ops(out.get("memory")), "user": _ops(out.get("user"))}
    log.info("done", memory_ops=len(ops["memory"]), user_ops=len(ops["user"]))
    return ops
