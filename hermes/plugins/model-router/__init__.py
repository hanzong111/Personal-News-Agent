"""model-router — pick the model per incoming chat message.

`pre_gateway_dispatch` fires once per message, before the agent turn. A Haiku judge (router.decide) labels
the message; "hard" sets this chat's session model override to Opus + medium reasoning, "easy" clears the
override so the configured default (Sonnet, low effort) runs. It uses the same per-session state that
/model and /reasoning write, and only ever touches values it set itself: a manual /model or /reasoning
choice wins until /new. Decisions are appended to ~/.hermes/logs/model-router.jsonl.

Trade-off: the prompt cache is per model, so a switch re-writes the (compressed) context once.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import importlib.util

_spec = importlib.util.spec_from_file_location("hermes_model_router_logic", Path(__file__).resolve().parent / "router.py")
router = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(router)

logger = logging.getLogger("plugins.model_router")
LOG = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser() / "logs" / "model-router.jsonl"
_last: dict[str, dict] = {}        # session_key -> {"label", "model_override", "reasoning_override"}


def _judge(prompt: str) -> str:
    from agent.auxiliary_client import call_llm
    resp = call_llm(provider="anthropic", model=router.JUDGE_MODEL, max_tokens=5, timeout=router.JUDGE_TIMEOUT_S,
                    messages=[{"role": "user", "content": prompt}])
    _judge.usage = getattr(resp, "usage", None)
    return resp.choices[0].message.content


_judge.usage = None


def _write_log(row: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _on_dispatch(event=None, gateway=None, session_store=None, **_):
    try:
        text = getattr(event, "text", None)
        source = getattr(event, "source", None)
        if gateway is None or source is None or not isinstance(text, str):
            return None
        norm = getattr(gateway, "_normalize_source_for_session_key", None)
        key = gateway._session_key_for_source(norm(source) if norm else source)
        conv = gateway._session_state(key).conversation
        mine = _last.get(key, {})
        # A /model or /reasoning the user set themselves (anything we didn't write) is respected.
        manual = (conv.model_override is not None and conv.model_override != mine.get("model_override")) or \
                 (conv.reasoning_override is not None and conv.reasoning_override != mine.get("reasoning_override"))
        if not manual and conv.model_override is None and session_store is not None:
            try:                                  # a /model saved before a restart is rehydrated after this hook
                manual = bool(session_store.get_model_override(key))
            except Exception:
                pass
        t0 = time.monotonic()
        _judge.usage = None
        label, how = router.decide(text, mine.get("label"), _judge)
        ms = int((time.monotonic() - t0) * 1000)
        if label is None or manual:
            if manual and label is not None:
                _write_log({"ts": time.time(), "session": key, "label": label, "how": "manual-override-kept",
                            "ms": ms, "text": text[:80]})
            return None
        if label == "hard":
            from hermes_constants import parse_reasoning_effort
            model_ov = {"model": router.HARD_MODEL, "provider": "anthropic"}
            reason_ov = parse_reasoning_effort(router.HARD_EFFORT)
        else:
            model_ov = reason_ov = None
        changed = conv.model_override != model_ov or conv.reasoning_override != reason_ov
        conv.model_override = dict(model_ov) if model_ov else None
        gateway._set_session_reasoning_override(key, reason_ov)
        if changed:
            gateway._evict_cached_agent(key)       # rebuild on the new model / effort
        _last[key] = {"label": label, "model_override": conv.model_override,
                      "reasoning_override": conv.reasoning_override}
        u = _judge.usage
        _write_log({"ts": time.time(), "session": key, "label": label, "how": how, "ms": ms,
                    "model": router.HARD_MODEL if label == "hard" else "default", "switched": changed,
                    "judge_in": getattr(u, "prompt_tokens", 0) if u else 0,
                    "judge_out": getattr(u, "completion_tokens", 0) if u else 0, "text": text[:80]})
    except Exception as e:                        # never block a message because routing failed
        logger.warning("model-router failed: %s", e)
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
