"""Jev — optional typed judgments from TypeSafe's System One model (https://docs.typesafe.ai/).

TickerPigeon runs with or without it. With `jev: on` in data/preferences.yaml, a TYPESAFE_API_KEY and the
`typesafe-sdk` package, these judgments replace fragile word-overlap and keyword rules:

    same_event(pairs)     do two headlines report the same event?     (repeats, story clustering)
    roles(pairs)          is the stock the subject of the headline?   (alert relevance)
    story_types(items)    earnings / contract / analyst / …           (story taxonomy)

Every function returns one answer per input, or None where Jev is off or a call failed; callers then use
their original rule for that input. Code finds the candidates and acts on the answers; Jev only judges.
Measured against the original rules in docs/benchmarks/jev.md.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline.log import get as _get_log

log = _get_log("jev")

MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")    # pinned: the thresholds below were measured on it
PRICE_PER_TOKEN = 0.042 / 1_000_000                  # USD per input token; output tokens are free
SAME_AT = 1.5          # same_event score above this = same event (0 different, 1 related, 2 same)
CONFIDENT = 0.7        # below this, a Choice answer is not trusted and the caller keeps its rule
WORKERS = 8

_state: dict = {}      # per-process cache: enabled flag, client


# ---------------------------------------------------------------- availability

def api_key() -> str | None:
    """TYPESAFE_API_KEY from the environment, else from ~/.hermes/.env or the project .env."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    root = Path(__file__).resolve().parent.parent
    hermes = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
    for env in (hermes / ".env", root / ".env"):
        try:
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"\'') or None
        except OSError:
            continue
    return None


def sdk_installed() -> bool:
    try:
        import typesafe_sdk  # noqa: F401
        return True
    except ImportError:
        return False


def status() -> tuple[bool, str]:
    """(usable, why) — for `setup status` and the wizard."""
    from pipeline import prefs
    try:
        wanted = prefs.load().get("jev", False)
    except ValueError:
        wanted = False
    if not wanted:
        return False, "off"
    if not sdk_installed():
        return False, "on, but typesafe-sdk is not installed (pip install -r requirements.txt)"
    if not api_key():
        return False, "on, but no TYPESAFE_API_KEY (add it to ~/.hermes/.env)"
    return True, f"on ({MODEL})"


def enabled() -> bool:
    if "enabled" not in _state:
        _state["enabled"], why = status()
        if why != "off" and not _state["enabled"]:
            log.warn("jev requested but unavailable; using the original rules", reason=why)
    return _state["enabled"]


def _client():
    if "client" not in _state:
        from typesafe_sdk import TypeSafeClient
        _state["client"] = TypeSafeClient(api_key=api_key(), model=MODEL)
    return _state["client"]


def _ask_many(requests: list[tuple[object, dict]], role: str) -> list[dict | None]:
    """Run (state, questions) requests in parallel. Answers as plain dicts; None for a failed request."""
    if not requests or not enabled():
        return [None] * len(requests)
    t0, tokens, failed = time.time(), [0], [0]

    def one(req):
        state, questions = req
        try:
            resp = _client().system_one(state=state, questions=questions)
        except Exception as e:  # noqa: BLE001 — any failure falls back to the rule for this input
            failed[0] += 1
            log.debug("jev request failed", role=role, err=f"{type(e).__name__}: {str(e)[:160]}")
            return None
        tokens[0] += resp.usage.input_tokens
        return {k: a.model_dump() for k, a in resp.answers.items()}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        out = list(pool.map(one, requests))
    log.info("call done", role=f"jev.{role}", model=MODEL, calls=len(requests), failed=failed[0],
             tok_in=tokens[0], tok_out=0, usd=round(tokens[0] * PRICE_PER_TOKEN, 6), dur=time.time() - t0)
    if failed[0]:
        log.warn("some jev requests failed; the original rule decided those", role=role, failed=failed[0])
    return out


# ---------------------------------------------------------------- questions (wording measured in the benchmark)

def _questions():
    from typesafe_sdk import Choice, Score
    same = Score(
        instructions="Do `headline_a` and `headline_b` report the same news event?",
        criteria=[
            "Different events: they are about different happenings, even if they share a company, sector or "
            "topic. For example two separate contracts won by the same company, results for different "
            "quarters, or two unrelated policy announcements.",
            "Related but not the same: one is a follow-up, reaction, analysis, market move or later "
            "development of the other's event. For example 'X wins RM2bil contract' and 'Analysts raise "
            "target price for X after contract win'.",
            "Same event: both report the same happening, possibly from different outlets, in different "
            "words or in different languages. For example 'X bags RM2bil job' and 'X secures RM2 billion "
            "contract from Y'.",
        ],
    )
    role = Choice(
        instructions="In `headline`, what role does the listed company described in `company` play?",
        criteria={
            "subject": "The headline reports news about this company itself: its results, contracts, deals, "
                       "share price or trading, management, lawsuits, regulatory action, plans or products.",
            "source_only": "The company appears only as the source of an opinion, forecast, trading call or "
                           "research about something else: its securities, research or investment-bank arm "
                           "commenting on the market, the economy, a commodity, an index, a sector or a "
                           "different company. Includes analyst and trading calls such as 'X stays short', "
                           "'X turns bearish on gold', 'X keeps buy call on Y' or 'Y upgraded by X'.",
            "passing_mention": "The company is one of several names listed, or is mentioned in passing, and the "
                               "headline is mainly about something else.",
            "different_entity": "The matching name refers to something other than this listed company: a "
                                "person, a place, a product, or a different organisation with a similar name.",
        },
    )
    story_type = Choice(
        instructions="What kind of stock-market news is `item`?",
        criteria={
            "earnings": "Quarterly or annual results, profit, revenue, guidance.",
            "contract": "A contract, tender, order or project won, lost or awarded to the company.",
            "corporate-action": "Dividends, rights issues, placements, buybacks, mergers, acquisitions, "
                                "disposals, land or asset purchases, sales and leases, listings, IPOs, "
                                "share splits.",
            "analyst": "A research house or analyst's rating, target price, recommendation or outlook.",
            "regulation": "A law, rule, policy, tariff or regulator's decision affecting companies.",
            "commodity": "Commodity prices or supply: oil, gas, palm oil, metals, electricity.",
            "macro": "The economy, interest rates, the ringgit, the market or index as a whole, the budget.",
            "management": "Directors, the CEO or chairman, appointments, resignations, boardroom changes.",
            "other": "None of the above.",
        },
    )
    return same, role, story_type


def _side(item: dict) -> dict:
    out = {"title": item.get("title", ""), "source": item.get("source") or item.get("src") or ""}
    if item.get("summary"):
        out["summary"] = str(item["summary"])[:240]
    return out


# ---------------------------------------------------------------- judgments

def same_event(pairs: list[tuple[dict, dict]], role: str = "same_event") -> list[float | None]:
    """Expected score 0..2 per pair (> SAME_AT = same event), or None."""
    if not pairs or not enabled():
        return [None] * len(pairs)
    same, _, _ = _questions()
    answers = _ask_many([({"headline_a": _side(a), "headline_b": _side(b)}, {"same": same}) for a, b in pairs], role)
    return [a["same"]["score"] if a else None for a in answers]


def roles(pairs: list[tuple[str, object]]) -> list[tuple[str, float] | None]:
    """(role, confidence) per (headline, holding), or None."""
    if not pairs or not enabled():
        return [None] * len(pairs)
    _, role, _ = _questions()
    reqs = [({"headline": title,
              "company": {"name": h.name, "ticker": h.short, "also_written_as": [a.strip() for a in h.aliases]}},
             {"role": role}) for title, h in pairs]
    return [(a["role"]["choice"], a["role"]["confidence"]) if a else None for a in _ask_many(reqs, "role")]


def story_types(items: list[dict]) -> list[tuple[str, float] | None]:
    """(type, confidence) per item, or None."""
    if not items or not enabled():
        return [None] * len(items)
    _, _, story_type = _questions()
    reqs = [({"item": {"headline": i.get("title", ""), **({"summary": str(i["summary"])[:300]} if i.get("summary") else {})}},
             {"type": story_type}) for i in items]
    return [(a["type"]["choice"], a["type"]["confidence"]) if a else None for a in _ask_many(reqs, "story_type")]
