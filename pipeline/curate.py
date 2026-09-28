"""Daily story curator and deterministic retention job.

Rules own all deletion decisions. The optional model can only suggest story assignments, types and
short notes; invalid or missing output falls back to deterministic behavior.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict

from agents import curator, jev
from . import config
from .memory import Memory
from .log import get as _get_log, new_run

log = _get_log("curate")


def _terms(title: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split() if len(w) > 3}


def _similar(a: str, b: str) -> float:
    left, right = _terms(a), _terms(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def _shared_scope(item: dict, story: dict) -> bool:
    codes = set(item.get("codes") or []) & set(story.get("codes") or [])
    if codes:
        return True
    return not item.get("codes") and bool(set(item.get("sectors") or []) & set(story.get("sectors") or []))


def _rule_story(item: dict, stories: list[dict]) -> dict | None:
    candidates = [(story, _similar(item.get("title", ""), story.get("title", "")))
                  for story in stories if _shared_scope(item, story)]
    candidates = [(story, score) for story, score in candidates if score >= 0.5]
    return max(candidates, key=lambda pair: pair[1])[0] if candidates else None


def _rule_type(item: dict) -> str:
    text = f"{item.get('title','')} {item.get('summary','')}".lower()
    rules = [
        ("earnings", ("profit", "earnings", "revenue", "quarter", "financial results")),
        ("contract", ("contract", "award", "order book", "tender", "project win")),
        ("corporate-action", ("dividend", "rights issue", "share split", "buyback", "acquisition", "disposal")),
        ("analyst", ("target price", "research", "upgrade", "downgrade", "recommendation")),
        ("regulation", ("regulation", "policy", "tariff", "bank negara", "ministry", "government")),
        ("commodity", ("oil price", "gas price", "coal", "copper", "steel price", "commodity")),
        ("management", ("director", "ceo", "chairman", "management", "resignation", "appointment")),
    ]
    return next((kind for kind, words in rules if any(word in text for word in words)),
                "macro" if item.get("macro") else "other")


def _deterministic_notes(memory: Memory, touched: set[str]) -> dict[str, dict]:
    delivered = memory.delivered(config.NOTE_RETENTION_DAYS)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in delivered:
        for key in set(item.get("codes") or []) | set(item.get("sectors") or []):
            if key in touched:
                grouped[key].append(item)
    out = {}
    for key in touched:
        seen_stories, words = set(), []
        for item in sorted(grouped.get(key, []), key=lambda i: i.get("published") or i.get("stage_at") or "", reverse=True):
            marker = item.get("story_id") or item["id"]
            if marker in seen_stories:
                continue
            seen_stories.add(marker)
            line = f"- {(item.get('published') or item.get('stage_at') or '')[:10]}: {item.get('brief_headline') or item['title']}"
            candidate = words + line.split()
            if len(candidate) > 80:
                break
            words = candidate
        if words:
            out[key] = {"kind": "holding" if key.isdigit() else "sector", "text": " ".join(words)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)
    new_run("curate")
    cfg = config.load()
    log.info("run start", mode="dry-run" if a.dry_run else "cron",
             llm=not a.no_llm and os.environ.get("CURATOR_LLM", "1") != "0")

    with Memory() as memory:
        items = memory.unclustered_delivered(7)
        stories = memory.open_stories(7)
        existing_matches: dict[str, dict] = {}
        batch_parent: dict[str, str] = {}
        batch_roots: list[dict] = []
        for item in items:
            story = _rule_story(item, stories)
            if story:
                existing_matches[item["id"]] = story
                item["_rule_story"] = story["id"]
                continue
            same_batch = _rule_story(item, batch_roots)
            if same_batch:
                batch_parent[item["id"]] = same_batch["_root_id"]
                item["_rule_story"] = f"new:{same_batch['_root_id']}"
            else:
                batch_roots.append({"id": f"new:{item['id']}", "_root_id": item["id"],
                                    "codes": item.get("codes") or [], "sectors": item.get("sectors") or [],
                                    "title": item.get("title", ""), "type": item.get("type", "")})
        unmatched = [item for item in items if item["id"] not in existing_matches and item["id"] not in batch_parent]
        rule_matches = len(existing_matches) + len(batch_parent)
        touched = {key for item in items for key in [*(item.get("codes") or []), *(item.get("sectors") or [])]}
        prune_plan = memory.prune(dry_run=True)
        if a.dry_run:
            plan = {"items": len(items), "rule_matches": rule_matches, "unmatched": len(unmatched),
                    "touched": sorted(touched), "prune": prune_plan}
            log.info("dry-run plan", **{k: v for k, v in plan.items() if k != "prune"})
            print(json.dumps(plan, indent=2, ensure_ascii=False))
            return

        model_out = {"assignments": {}, "notes": {}}
        use_llm = bool(items) and not a.no_llm and os.environ.get("CURATOR_LLM", "1") != "0"
        if use_llm:
            old_notes = memory.notes_for(touched)
            try:
                with log.span("llm", items=len(items), unmatched=len(unmatched)):
                    model_out = curator.run(cfg, items, stories, old_notes)
            except Exception as e:
                log.error("curator failed; using rules", err=f"{type(e).__name__}: {e}")

        # Story type when neither the briefer nor the curator gave one: Jev if on, else the keyword rules.
        untyped = [item for item in items if not item.get("type")]
        jev_types = {item["id"]: ans[0] for item, ans in zip(untyped, jev.story_types(untyped)) if ans}
        if jev_types:
            log.info("jev story types", items=len(untyped), typed=len(jev_types))

        def fallback_type(item: dict) -> str:
            return jev_types.get(item["id"]) or _rule_type(item)

        with log.span("cluster", items=len(items), rule_matches=rule_matches, unmatched=len(unmatched)):
            resolved: dict[str, str] = {}
            for item in items:
                if item["id"] in existing_matches:
                    story = existing_matches[item["id"]]
                    memory.attach_story(item, story["id"], item.get("type") or story.get("type") or fallback_type(item))
                    resolved[item["id"]] = story["id"]
            deferred: list[tuple[dict, dict, str]] = []
            for item in unmatched:
                assignment = model_out["assignments"].get(item["id"], {})
                story_type = assignment.get("type") or item.get("type") or fallback_type(item)
                target = assignment.get("story_id") or "new"
                if target.startswith("new:"):
                    deferred.append((item, assignment, story_type))
                    continue
                if target != "new":
                    if memory.attach_story(item, assignment["story_id"], story_type):
                        resolved[item["id"]] = assignment["story_id"]
                        continue
                resolved[item["id"]] = memory.create_story(item, story_type)
            for item, assignment, story_type in deferred:
                parent = assignment["story_id"].removeprefix("new:")
                story_id = resolved.get(parent)
                if story_id and memory.attach_story(item, story_id, story_type):
                    resolved[item["id"]] = story_id
                else:
                    resolved[item["id"]] = memory.create_story(item, story_type)
            for item in items:
                parent = batch_parent.get(item["id"])
                if not parent:
                    continue
                story_id = resolved.get(parent)
                if story_id:
                    memory.attach_story(item, story_id, item.get("type") or fallback_type(item))
                    resolved[item["id"]] = story_id
                else:
                    resolved[item["id"]] = memory.create_story(item, item.get("type") or fallback_type(item))

        with log.span("notes", keys=len(touched)):
            notes = _deterministic_notes(memory, touched)
            notes.update(model_out.get("notes") or {})
            for key, note in notes.items():
                memory.set_note(key, note["kind"], note["text"])

        with log.span("prune"):
            pruned = memory.prune()
        log.info("prune counts", **{k: v for k, v in pruned.items() if k != "at"})
        log.info("silent", clustered=len(items), stories=len(memory.open_stories(config.DELIVERED_RETENTION_DAYS)),
                 notes=len(notes))


if __name__ == "__main__":
    main()
