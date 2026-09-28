"""STOCK BRIEFER — Sonnet. One JSON verdict per holding news item. Does NOT write the message.

  input : tier-1 items [{id, codes, source, title, summary, published, impact, kind}] + portfolio
  output: {id: {keep, type, sentiment, risk, headline, summary, why}}
"""
from __future__ import annotations
from . import llm
from pipeline.log import get as _get_log

log = _get_log("agent.briefer")

TYPES = ["earnings", "contract", "corporate-action", "analyst", "regulation", "commodity", "macro", "management", "other"]
SENTS = ["pos", "neg", "neu"]

PROMPT = """You brief a Bursa Malaysia shareholder on news about stocks they hold. For EVERY item below return one JSON object. Output a JSON array only — no prose, no fences.

Fields:
  "id":        copy exactly
  "keep":      false if the item is not worth an alert: it reports the same concrete event already
               present in RECENT CONTEXT (even when the wording/source differs); the holding is
               merely the author of research/economic commentary about other things; a recycled or
               evergreen page whose content is clearly old (old election tags like #GE14, "years
               ago"); a headline with no concrete development ("X stamps its mark"); a stock-picks
               listicle; a bare ticker page. Otherwise true.
  "type":      earnings | contract | corporate-action | analyst | regulation | commodity | macro | management | other
  "sentiment": pos | neg | neu   (for the shareholder)
  "risk":      true only if it signals a material risk (profit warning, lock-up expiry, regulatory action, lawsuit, failed bid)
  "headline":  4-8 words, English, names and numbers exact
  "summary":   1-2 sentences, English, concrete (amounts, dates, counterparties). Translate Chinese/Malay.
  "why":       one line: why it matters to a shareholder of this stock
  "skip_reason": short reason when keep=false, else ""

HOLDINGS (entries marked WATCHLIST are not held: the user is watching them as possible buys, so
frame "sentiment" and "why" for someone considering the stock rather than a shareholder):
{holdings}

RECENT CONTEXT (may be empty; use only when directly relevant):
{notes}

ITEMS (id | codes | impact | source | published | title || summary):
{lines}"""


def run(cfg, items: list[dict], notes: dict[str, str] | None = None) -> dict[str, dict]:
    if not items:
        return {}
    holdings = "\n".join(f"- {h.code} {h.short}: {h.name} ({cfg.sectors[h.sector].label})" + (" [WATCHLIST — not held]" if h.watch else "") for h in cfg.holdings)
    note_lines = "\n".join(f"- {key}: {text}" for key, text in sorted((notes or {}).items())) or "(none)"
    lines = "\n".join(f"{i['id']} | {','.join(i['codes'])} | {i.get('impact', 'normal')} | {i['source'][:28]} | {i['published'][:16]} | "
                      f"{i['title'][:140]} || {(i.get('summary') or '')[:300]}" for i in items)
    log.info("start", items=len(items))
    out = llm.call_json(PROMPT.format(holdings=holdings, notes=note_lines, lines=lines), model=llm.SONNET, effort="low", role="briefer")
    ids = {i["id"] for i in items}
    res: dict[str, dict] = {}
    for row in out if isinstance(out, list) else []:
        i = str(row.get("id", ""))
        if i not in ids:
            continue
        res[i] = {"keep": bool(row.get("keep", True)),
                  "type": row.get("type") if row.get("type") in TYPES else "other",
                  "sentiment": row.get("sentiment") if row.get("sentiment") in SENTS else "neu",
                  "risk": bool(row.get("risk", False)),
                  "headline": str(row.get("headline") or "").strip()[:90],
                  "summary": str(row.get("summary") or "").strip()[:400],
                  "why": str(row.get("why") or "").strip()[:200],
                  "skip_reason": str(row.get("skip_reason") or "").strip()[:80]}
    for i in items:
        if i["id"] not in res:
            log.warn("item not briefed (kept with raw title)", id=i["id"], title=i["title"][:60])
            res[i["id"]] = {"keep": True, "type": "other", "sentiment": "neu", "risk": False,
                            "headline": i["title"][:90], "summary": i.get("summary", "")[:400], "why": "", "skip_reason": ""}
    kept = sum(1 for v in res.values() if v["keep"])
    for i in items:
        v = res[i["id"]]
        (log.info if v["keep"] else log.info)("verdict", id=i["id"], keep=v["keep"], type=v["type"], sentiment=v["sentiment"],
                                             risk=v["risk"], title=i["title"][:60], skip=v["skip_reason"] or "-")
    log.info("done", items=len(items), kept=kept, skipped=len(items) - kept)
    return res
