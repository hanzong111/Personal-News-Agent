"""LLM backend for the agents.

    call(prompt, model=..., effort=...) -> str
    call_json(prompt, ...)              -> parsed JSON (dict | list)

Backends (env AGENT_BACKEND, default 'hermes'):
  hermes     one-shot `hermes chat -Q` with tools/memory/rules off. Uses whatever credentials
             Hermes has. With toolsets disabled the Hermes overhead is ~800 tokens per call.
  anthropic  direct Anthropic SDK (needs ANTHROPIC_API_KEY and `pip install anthropic`).
             No overhead; cheapest. Same prompts, same JSON contract.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import subprocess
import time
from pipeline.log import get as _get_log

log = _get_log("llm")

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"

BACKEND = os.environ.get("AGENT_BACKEND", "hermes")
PROVIDER = os.environ.get("AGENT_PROVIDER", "anthropic")
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
STATE_DB = os.path.join(HERMES_HOME, "state.db")
PRICE = {"claude-opus-5": (5, 25, .5, 6.25), "claude-sonnet-5": (2, 10, .2, 2.5), "claude-haiku-4-5": (1, 5, .1, 1.25)}


def _log(msg: str):
    log.debug(msg)


def _ledger(session_id: str) -> dict:
    """Token usage Hermes recorded for a one-shot session (model actually used, in/out, cost estimate)."""
    try:
        c = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        r = c.execute("select model, input_tokens, cache_write_tokens, cache_read_tokens, output_tokens "
                      "from sessions where id=?", (session_id,)).fetchone()
        c.close()
        if not r:
            return {}
        model, inp, cw, cr, out = r
        p = PRICE.get(model, (0, 0, 0, 0))
        return {"model_used": model, "tok_in": inp + cw + cr, "tok_out": out,
                "usd": round((inp * p[0] + out * p[1] + cr * p[2] + cw * p[3]) / 1e6, 4)}
    except Exception as e:
        log.warn("ledger lookup failed", err=str(e))
        return {}


def _hermes(prompt: str, model: str, effort: str, timeout: int) -> str:
    cmd = ["hermes", "chat", "-Q", "--query-file", "-", "--model", model, "--provider", PROVIDER,
           "--reasoning", effort, "--ignore-rules", "--max-turns", "1", "-t", "none"]   # "none" = no toolsets -> ~800-token system prompt
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd="/tmp")
    m = re.search(r"session_id:\s*(\S+)", r.stderr)
    _hermes.last_session = m.group(1) if m else None
    if r.returncode != 0:
        raise RuntimeError(f"hermes chat failed ({r.returncode}): {r.stderr[-500:]}")
    return r.stdout.strip()


_hermes.last_session = None


def _anthropic(prompt: str, model: str, effort: str, timeout: int) -> str:
    import anthropic  # optional dependency
    client = anthropic.Anthropic(timeout=timeout)
    with client.messages.stream(model=model, max_tokens=16000,
                                thinking={"type": "adaptive"}, output_config={"effort": effort},
                                messages=[{"role": "user", "content": prompt}]) as stream:
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def call(prompt: str, model: str = SONNET, effort: str = "low", timeout: int = 300, role: str = "") -> str:
    t0 = time.time()
    fn = _anthropic if BACKEND == "anthropic" else _hermes
    log.debug("call start", role=role, model=model, backend=BACKEND, prompt_chars=len(prompt))
    try:
        out = fn(prompt, model, effort, timeout)
    except Exception as e:
        log.error("call failed", role=role, model=model, backend=BACKEND, dur=time.time() - t0, err=f"{type(e).__name__}: {e}")
        raise
    usage = _ledger(_hermes.last_session) if BACKEND == "hermes" and _hermes.last_session else {}
    log.info("call done", role=role, model=model, backend=BACKEND, prompt_chars=len(prompt), reply_chars=len(out),
             dur=time.time() - t0, session=_hermes.last_session or "", **usage)
    return out


def parse_json(text: str):
    """Tolerant JSON extraction: strips ``` fences and any prose around the first JSON value."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON in reply: {text[:200]!r}")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(t[start:])
    return obj


def call_json(prompt: str, retries: int = 1, **kw):
    last = None
    for attempt in range(retries + 1):
        try:
            return parse_json(call(prompt, **kw))
        except (ValueError, json.JSONDecodeError) as e:
            last = e
            log.warn("bad JSON from model", attempt=attempt + 1, err=str(e)[:120])
    raise last
