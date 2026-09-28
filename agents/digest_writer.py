"""DIGEST WRITER — one Sonnet pass that groups the evening queue into stories and writes the words.

It never writes URLs, times or prices: it returns item ids, and `renderer.render_digest` fills those in
from the stored items (an agent writing the whole message once linked the wrong article — 2026-09-23).

  input : digest items [{id, codes, sectors, macro, mention, source, title, summary, published, judge_why}]
  output: [{section, item_ids, sentiment, headline, summary, touches}]  (empty list = nothing worth sending)
"""
from __future__ import annotations

from . import llm
from pipeline.log import get as _get_log

log = _get_log("agent.digest_writer")
SENTS = {"pos", "neg", "neu", "risk"}

PROMPT = """You write the evening digest for a Bursa Malaysia investor. Below is today's queue of sector and
macro news that may touch industries they hold. Return one JSON object only:
  {{"stories": [{{"section": "<one of: {sections}>", "item_ids": ["id", ...], "sentiment": "pos|neg|neu|risk",
                 "headline": "4-8 words", "summary": "1-2 sentences", "touches": "which holdings and how"}}]}}

- Merge items about the same story into one entry (list all their ids, most informative first).
- sentiment is for THEIR holdings: pos positive · neg negative · neu neutral or mixed · risk uncertain/risk.
- Items marked (mention only) name a holding in passing; include only if the mention is meaningful.
- Drop pure noise: generic market wrap-ups, unrelated companies, foreign news with no Malaysian angle.
- Translate Chinese / Malay into English. Plain facts from the items only; no URLs, no prices, no times.
- Keep it short: at most 8 stories, most important first. If nothing is worth sending: {{"stories": []}}

HOLDINGS (code short name — sector; WATCHLIST entries are watched, not held — say "watchlist" in touches):
{holdings}

ITEMS (id | section | holdings named | mention? | source | title || summary || relevance):
{items}
"""


def _section(item: dict) -> str:
    if item.get("sectors"):
        return item["sectors"][0]
    return "macro"


def run(cfg, items: list[dict]) -> list[dict]:
    if not items:
        return []
    sections = [k for k in cfg.sectors if cfg.holdings_in_sector(k)] + ["macro"]
    holdings = "\n".join(f"- {h.code} {h.short} {h.name} — {h.sector}" + (" [WATCHLIST — not held]" if h.watch else "") for h in cfg.holdings)
    lines = "\n".join(
        f"{i['id']} | {_section(i)} | {','.join(i.get('codes') or []) or '-'} | {'mention' if i.get('mention') else '-'} | "
        f"{i['source'][:30]} | {i['title'][:160]} || {(i.get('summary') or '')[:280]} || {(i.get('judge_why') or '')[:120]}"
        for i in items)
    log.info("start", items=len(items))
    out = llm.call_json(PROMPT.format(sections=", ".join(sections), holdings=holdings, items=lines),
                        model=llm.SONNET, effort="low", role="digest_writer")
    ids = {i["id"] for i in items}
    stories, used = [], set()
    for row in (out.get("stories") if isinstance(out, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        item_ids = [str(x) for x in row.get("item_ids") or [] if str(x) in ids and str(x) not in used]
        if not item_ids or not str(row.get("headline") or "").strip():
            continue                                  # unknown / reused ids never reach the renderer
        used.update(item_ids)
        section = row.get("section") if row.get("section") in sections else "macro"
        stories.append({"section": section, "item_ids": item_ids,
                        "sentiment": row.get("sentiment") if row.get("sentiment") in SENTS else "neu",
                        "headline": str(row["headline"]).strip(), "summary": str(row.get("summary") or "").strip(),
                        "touches": str(row.get("touches") or "").strip()})
    log.info("done", stories=len(stories), items_used=len(used), items=len(items))
    return stories
