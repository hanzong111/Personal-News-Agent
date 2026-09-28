"""Relevance gate. Pure rules, no LLM.

tier 1 = a holding is named in the TITLE, or it's a Bursa filing for a holding  -> immediate alert
tier 2 = sector / macro news, or a holding only mentioned in the body          -> evening digest
tier 0 = nothing matched                                                        -> dropped
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta
from . import config
from .config import Config
from .fetchers import MYT


def _alias_hit(text: str, aliases: list[str]) -> bool:
    t = text.lower()
    for a in aliases:
        a = a.lower()
        if a.endswith(" "):                      # trailing-space alias = whole-word match
            if re.search(r"\b" + re.escape(a.strip()) + r"\b", t):
                return True
        elif a in t:
            return True
    return False


# An alias that appears only as the SOURCE of a statement ("says CIMB Securities", "RHB Research:",
# "— CIMB", "according to RHB") is not news about the company. Any hit outside these shapes counts.
_ATTRIB = [
    r"(?:says|said|according to|per|by|via|from|cites?|quotes?)\s+{a}(?:\s+(?:securities|research|investment bank|ib|economics|analysts?|group))?\b",
    r"\b{a}(?:\s+(?:securities|research|investment bank|ib|economics|analysts?))?\s*(?::|—|–|-)\s",
    r"(?::|—|–|-)\s*{a}(?:\s+(?:securities|research|investment bank|ib|economics|analysts?))?\s*$",
    # broker/economist voice: "CIMB sees…", "RHB keeps bearish bias", "CIMB Securities upgrades…"
    r"\b{a}(?:\s+(?:securities|research|investment bank|ib|economics|analysts?))?\s+(?:says|said|sees|expects|keeps|maintains|stays|remains|upgrades|downgrades|raises|cuts|trims|lifts|initiates|reiterates|forecasts|projects|predicts|tips|upbeat|bullish|bearish|positive|neutral|negative)\b",
]


def attribution_only(title: str, aliases: list[str]) -> bool:
    """True if every alias mention in the title is an attribution (source), not the subject."""
    t = title.lower()
    for a in aliases:
        a = a.strip().lower()
        if not a or a not in t:
            continue
        stripped = t
        for pat in _ATTRIB:
            stripped = re.sub(pat.format(a=re.escape(a)), " ", stripped)
        if a in stripped:          # some mention survives outside an attribution shape -> real subject
            return False
    return True


def title_key(title: str) -> str:
    """Normalised title for cross-source dedup (same article via KLSE Screener and Google News)."""
    return "t:" + re.sub(r"[^a-z0-9一-鿿]+", "", title.lower())[:80]


def _terms(title: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", title.lower()).split() if len(w) > 3}


def is_repeat(item: dict, recent: list[dict], threshold: float = 0.5) -> dict | None:
    """Return a recent alert for the same holding/story, or None."""
    terms = _terms(item["title"])
    if not terms:
        return None
    for row in recent:
        if set(item.get("codes") or []) & set(row.get("codes") or []):
            prior = set(row.get("terms") or _terms(row.get("title", "")))
            if prior and len(terms & prior) / len(terms | prior) >= threshold:
                return row
    return None


def tag(item: dict, cfg: Config) -> dict:
    """Decide codes / sectors / tier from title + summary. Mutates and returns the item."""
    title = item.get("title", "")
    body = f"{title} {item.get('summary', '')}"
    feed_codes = set(item.get("codes") or [])

    title_codes = {h.code for h in cfg.holdings if _alias_hit(title, h.aliases) and not attribution_only(title, h.aliases)}
    if item.get("kind") == "announcement":
        strong = feed_codes | title_codes
    else:
        strong = title_codes
    weak = (feed_codes | {h.code for h in cfg.holdings if _alias_hit(body, h.aliases)}) - strong

    sectors = set(item.get("sectors") or [])
    for key, s in cfg.sectors.items():
        if _alias_hit(body, s.keywords):
            sectors.add(key)
    sectors = {s for s in sectors if cfg.holdings_in_sector(s)}   # only sectors we hold

    if strong:
        item["tier"], item["codes"], item["mention"] = 1, sorted(strong), False
    elif weak or sectors or item.get("macro"):
        item["tier"], item["codes"], item["mention"] = 2, sorted(weak), bool(weak)
    else:
        item["tier"], item["codes"], item["mention"] = 0, [], False
    item["sectors"] = sorted(sectors)
    item["tkey"] = title_key(title)
    return item


def dedup_titles(items: list[dict]) -> list[dict]:
    """Keep one item per normalised title, preferring the copy that has a summary."""
    best: dict[str, dict] = {}
    for it in items:
        k = it["tkey"]
        if k not in best or (it.get("summary") and not best[k].get("summary")):
            best[k] = it
    return list(best.values())


def is_fresh(item: dict) -> bool:
    try:
        pub = datetime.fromisoformat(item["published"])
    except Exception:
        return True
    return pub >= datetime.now(MYT) - timedelta(hours=config.LOOKBACK_HOURS)
