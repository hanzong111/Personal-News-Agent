"""Shared plumbing for the Jev experiments: API key lookup, an answer cache, and cost tracking.

Every answer is cached under benchmarks/jev/.cache/ keyed by (model, state, questions), so re-running
an experiment to tweak the report costs nothing. Delete the folder to ask again.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CACHE = HERE / ".cache"
RESULTS = HERE / "results"
MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")     # pinned: thresholds tuned here belong to this version
PRICE_PER_TOKEN = 0.042 / 1_000_000                   # USD per input token (docs.typesafe.ai/models); output is free

sys.path.insert(0, str(ROOT))                          # so `pipeline` imports work when run as a script


def api_key() -> str | None:
    """TYPESAFE_API_KEY from the environment, else from ~/.hermes/.env or the project .env."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    hermes_home = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
    for env in (hermes_home / ".env", ROOT / ".env"):
        try:
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"\'') or None
        except OSError:
            continue
    return None


def _key(state, questions: dict) -> str:
    blob = json.dumps({"model": MODEL, "state": state,
                       "questions": {k: q.model_dump() if hasattr(q, "model_dump") else q for k, q in questions.items()}},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class Jev:
    """Cached, cost-tracked wrapper around TypeSafeClient.system_one. `answers` come back as plain dicts."""

    def __init__(self):
        from typesafe_sdk import TypeSafeClient
        key = api_key()
        if not key:
            raise SystemExit("No TYPESAFE_API_KEY. Add it to ~/.hermes/.env (TYPESAFE_API_KEY=…) or export it, "
                             "or run with --dry-run. Get a key at https://console.typesafe.ai/")
        self.client = TypeSafeClient(api_key=key, model=MODEL)
        self.tokens = 0
        self.calls = 0
        self.cached = 0
        CACHE.mkdir(exist_ok=True)

    def ask(self, state, questions: dict) -> dict:
        path = CACHE / f"{_key(state, questions)}.json"
        if path.exists():
            self.cached += 1
            return json.loads(path.read_text())["answers"]
        resp = self.client.system_one(state=state, questions=questions)
        self.calls += 1
        self.tokens += resp.usage.input_tokens
        record = {"model": resp.model, "input_tokens": resp.usage.input_tokens,
                  "answers": {k: a.model_dump() for k, a in resp.answers.items()}}
        path.write_text(json.dumps(record, ensure_ascii=False))
        return record["answers"]

    def cost_line(self) -> str:
        return (f"{self.calls} API calls ({self.cached} from cache), {self.tokens:,} input tokens, "
                f"≈ US${self.tokens * PRICE_PER_TOKEN:.4f}")


def estimate(payloads: list[tuple[object, dict]]) -> str:
    """Rough dry-run estimate: ~4 characters per token."""
    chars = sum(len(json.dumps(s, ensure_ascii=False)) +
                len(json.dumps({k: q.model_dump() for k, q in qs.items()}, ensure_ascii=False)) for s, qs in payloads)
    tokens = chars // 4
    return f"{len(payloads)} requests, ~{tokens:,} input tokens, ≈ US${tokens * PRICE_PER_TOKEN:.4f}"
