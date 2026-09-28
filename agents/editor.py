"""EDITOR — Sonnet. Decides what leads and what's worth a line. Does NOT write the message.

  input : classified Malaysian stories [{id, section, sentiment, risk, title_en, reports}]
  output: {"threads": [{"name", "story_ids", "developments"}], "highlights": {SECTION: [ids]}, "collapse": [SECTION]}
"""
from __future__ import annotations
from . import llm
from .classifier import SECTIONS
from pipeline.log import get as _get_log

log = _get_log("agent.editor")

PROMPT = """You are the editor of a Malaysian headline index. Below are today's stories, already classified. Decide the structure; another program does the layout. Output ONE JSON object only — no prose, no markdown fences.

Rules:
- "threads": 2 to 4 objects, the biggest stories of the window. A thread is either one story with many reports, or a cluster of related stories about one saga (e.g. many statements on the same political crisis). Rank threads by total reports, then by risk. Fields:
    "name": 4-8 words naming the saga/event (not a headline copy)
    "story_ids": every story that belongs to the thread (all of them, so they are not repeated elsewhere)
    "developments": up to 3 story ids that best summarise the thread, most material first. Each must add a DIFFERENT fact — never two ids that say the same thing in different words
- "highlights": for each section, up to 3 story ids NOT already in a thread, most newsworthy first (risk and reports count; skip routine items when better ones exist). Never pick two ids about the same event — if two titles say the same thing, take one. Omit sections with nothing left.
- "collapse": sections to show as a count only. Always include SPORT and OTHER unless one has a genuinely notable story (a first/major medal, a national record, a major cultural event) — in that case leave it out of collapse and highlight that story.
- Use only the ids given. Never invent ids.

Stories (id | section | sentiment | risk | reports | title):
{lines}"""


def run(stories: list[dict]) -> dict:
    if not stories:
        log.info("nothing to edit")
        return {"threads": [], "highlights": {}, "collapse": ["SPORT", "OTHER"]}
    log.info("start", stories=len(stories))
    lines = "\n".join(f"{s['id']} | {s['section']} | {s['sentiment']} | {'RISK' if s['risk'] else '-'} | x{s['reports']} | {s['title_en']}"
                      for s in stories)
    out = llm.call_json(PROMPT.format(lines=lines), model=llm.SONNET, effort="low", role="editor")
    ids = {s["id"] for s in stories}
    plan = {"threads": [], "highlights": {}, "collapse": []}
    used: set[str] = set()
    for t in (out.get("threads") or [])[:4]:
        sids = [i for i in map(str, t.get("story_ids", [])) if i in ids and i not in used]
        if not sids:
            continue
        devs = [i for i in map(str, t.get("developments", [])) if i in sids][:3] or sids[:3]
        used.update(sids)
        plan["threads"].append({"name": str(t.get("name", "")).strip()[:60] or "Top story", "story_ids": sids, "developments": devs})
    for sec, sids in (out.get("highlights") or {}).items():
        sec = str(sec).upper()
        if sec in SECTIONS:
            keep = [i for i in map(str, sids) if i in ids and i not in used][:3]
            if keep:
                plan["highlights"][sec] = keep
                used.update(keep)
    plan["collapse"] = [s for s in map(str.upper, out.get("collapse") or []) if s in SECTIONS]

    # a thread needs weight: >= 3 reports or >= 2 stories; otherwise demote its stories to highlights
    by_id = {s["id"]: s for s in stories}
    keep = []
    for t in plan["threads"]:
        weight = sum(by_id[i]["reports"] for i in t["story_ids"] if i in by_id)
        if weight >= 3 or len(t["story_ids"]) >= 2:
            keep.append(t)
        else:
            for i in t["story_ids"]:
                sec = by_id[i]["section"]
                hl = plan["highlights"].setdefault(sec, [])
                if i not in hl:
                    hl.insert(0, i)
                plan["highlights"][sec] = hl[:3]
    dropped = len(plan["threads"]) - len(keep)
    plan["threads"] = keep
    log.info("plan", threads=len(keep), thin_threads_demoted=dropped,
             highlights=sum(len(v) for v in plan["highlights"].values()), collapse=",".join(plan["collapse"]) or "-",
             thread_names=" | ".join(t["name"] for t in keep))
    return plan
