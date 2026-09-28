"""CURATOR — one optional Haiku pass for semantic story merges, taxonomy and rolling notes."""
from __future__ import annotations

from . import llm
from pipeline.log import get as _get_log

log = _get_log("agent.curator")
TYPES = {"earnings", "contract", "corporate-action", "analyst", "regulation", "commodity", "macro", "management", "other"}

PROMPT = """You maintain compact memory for a Bursa Malaysia portfolio news service.
Return one JSON object only, with exactly these top-level fields:
  "assignments": [{{"id":"item id","story_id":"existing story id or new","type":"taxonomy value"}}]
  "notes": [{{"key":"holding code or sector key","kind":"holding|sector","text":"<=80 words"}}]

For every UNMATCHED item, reuse an existing story only when it is the same concrete event despite
different wording. To group two unmatched items into one new story, make the later item use
"new:<canonical item id>"; otherwise use "new". Categorise it as one of: earnings, contract,
corporate-action, analyst, regulation, commodity, macro, management, other.

For every TOUCHED key, rewrite its old note using today's items: factual dated bullets, newest first,
maximum 80 words total. Preserve still-relevant facts from the old note; omit anything older than
60 days. Do not infer facts not present in the input.

HOLDINGS:
{holdings}

SECTORS:
{sectors}

OPEN STORIES (id | type | codes | sectors | title):
{stories}

TODAY'S ITEMS (id | rule story or unmatched | codes | sectors | date | title | summary):
{items}

OLD NOTES:
{notes}
"""


def run(cfg, items: list[dict], stories: list[dict], notes: dict[str, str]) -> dict:
    if not items:
        return {"assignments": {}, "notes": {}}
    holdings = "\n".join(f"- {h.code} {h.short}: {h.name}" + (" [WATCHLIST — not held]" if h.watch else "") for h in cfg.holdings)
    sectors = "\n".join(f"- {key}: {sector.label}" for key, sector in cfg.sectors.items()
                         if cfg.holdings_in_sector(key))
    story_lines = "\n".join(
        f"{s['id']} | {s.get('type') or 'other'} | {','.join(s.get('codes') or []) or '-'} | "
        f"{','.join(s.get('sectors') or []) or '-'} | {s.get('title','')[:140]}" for s in stories) or "(none)"
    item_lines = "\n".join(
        f"{i['id']} | {i.get('_rule_story') or 'unmatched'} | {','.join(i.get('codes') or []) or '-'} | "
        f"{','.join(i.get('sectors') or []) or '-'} | {(i.get('published') or '')[:10]} | {i.get('title','')[:140]} | "
        f"{(i.get('summary') or '')[:200]}" for i in items)
    note_lines = "\n".join(f"{key}: {text}" for key, text in sorted(notes.items())) or "(none)"
    log.info("start", items=len(items), stories=len(stories), notes=len(notes))
    out = llm.call_json(PROMPT.format(holdings=holdings, sectors=sectors, stories=story_lines,
                                      items=item_lines, notes=note_lines),
                        model=llm.HAIKU, effort="low", role="curator")
    existing = {s["id"] for s in stories}
    item_ids = {i["id"] for i in items if not i.get("_rule_story")}
    valid_keys = {h.code for h in cfg.holdings} | {k for k in cfg.sectors if cfg.holdings_in_sector(k)}
    assignments = {}
    for row in out.get("assignments", []) if isinstance(out, dict) else []:
        item_id = str(row.get("id") or "")
        if item_id not in item_ids:
            continue
        story_id = str(row.get("story_id") or "new")
        new_target = story_id.removeprefix("new:") if story_id.startswith("new:") else ""
        assignments[item_id] = {
            "story_id": story_id if story_id in existing or (new_target in item_ids and new_target != item_id) else "new",
            "type": row.get("type") if row.get("type") in TYPES else "other",
        }
    note_out = {}
    for row in out.get("notes", []) if isinstance(out, dict) else []:
        key = str(row.get("key") or "")
        text = " ".join(str(row.get("text") or "").split())
        if key in valid_keys and text:
            note_out[key] = {"kind": "holding" if key.isdigit() else "sector",
                             "text": " ".join(text.split()[:80])}
    log.info("done", assignments=len(assignments), notes=len(note_out))
    return {"assignments": assignments, "notes": note_out}
