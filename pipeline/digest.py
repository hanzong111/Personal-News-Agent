"""Evening digest with lossless pending stages and chunked relevance judging."""
from __future__ import annotations

import argparse
import json
import sys

from . import config, fetchers, hold
from .match import is_fresh
from .memory import Memory
from .render import render, render_prices
from agents import digest_writer, judge
from agents.renderer import render_digest
from datetime import datetime
from .log import get as _get_log, new_run

log = _get_log("digest")
SILENT = json.dumps({"wakeAgent": False})
MAX_ITEMS = 40
JUDGE_CHUNK = 60


def _emit(text: str) -> None:
    if text != SILENT:                          # silent ticks have no message to time
        hold.until_target("digest")
    sys.stdout.write(text.rstrip() + "\n")
    sys.stdout.flush()


def _judge_chunks(cfg, candidates: list[dict], memory: Memory, run_id: str, persist: bool) -> list[dict]:
    rescued: list[dict] = []
    total_chunks = (len(candidates) + JUDGE_CHUNK - 1) // JUDGE_CHUNK
    if len(candidates) > JUDGE_CHUNK:
        log.warn("judge overflow chunked", candidates=len(candidates), chunk=JUDGE_CHUNK, chunks=total_chunks)
    for n in range(0, len(candidates), JUDGE_CHUNK):
        chunk = candidates[n:n + JUDGE_CHUNK]
        chunk_no = n // JUDGE_CHUNK + 1
        try:
            verdicts = judge.run(cfg, chunk)
        except Exception as e:
            log.error("judge failed", chunk=f"{chunk_no}/{total_chunks}", candidates=len(chunk),
                      err=f"{type(e).__name__}: {e}")
            break
        for candidate in chunk:
            verdict = verdicts.get(candidate["id"])
            if verdict:
                candidate.update(codes=verdict["codes"], sectors=verdict["sectors"], tier=2,
                                 mention=bool(verdict["codes"]), judge_why=verdict["why"])
                rescued.append(candidate)
            if persist:
                memory.judge_result(candidate["id"], verdict, run_id)
        log.info("judge chunk", chunk=f"{chunk_no}/{total_chunks}", candidates=len(chunk), rescued=len(verdicts))
    return rescued


def _fallback(items: list[dict]) -> list[dict]:
    """Writer down: one plain story per item (title as headline) so the digest still goes out, links intact."""
    return [{"section": (i.get("sectors") or ["macro"])[0], "item_ids": [i["id"]], "sentiment": "neu",
             "headline": i["title"][:120], "summary": "", "touches": ""} for i in items[:12]]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--raw", action="store_true", help="print the raw item block instead of the written digest")
    a = ap.parse_args(argv)
    run_id = new_run("digest")
    mode = "preview" if a.preview else "dry-run" if a.dry_run else "cron"
    log.info("run start", mode=mode)
    cfg = config.load()

    with Memory() as memory:
        if a.preview:
            from .scan import collect
            items = [item for item in collect(cfg) if item["tier"] == 2]
            candidates: list[dict] = []
        else:
            items = memory.pending("digest")
            candidates = memory.pending("judge")

        stale_candidates = [candidate for candidate in candidates if not is_fresh(candidate)]
        candidates = [candidate for candidate in candidates if is_fresh(candidate)]
        if stale_candidates and not a.dry_run and not a.preview:
            memory.advance([i["id"] for i in stale_candidates], "dropped", "stale", run_id)
        rescued = _judge_chunks(cfg, candidates, memory, run_id, persist=not a.dry_run and not a.preview) if candidates else []
        if a.dry_run:
            items += rescued
        elif not a.preview:
            items = memory.pending("digest")

        held = {h.code for h in cfg.holdings}
        stale = [i for i in items if not is_fresh(i)]
        invalid = [i for i in items if i not in stale and not (i.get("sectors") or i.get("macro") or set(i.get("codes") or []) & held)]
        if not a.dry_run and not a.preview:
            memory.advance([i["id"] for i in stale], "dropped", "stale", run_id)
            memory.advance([i["id"] for i in invalid], "dropped", "no_longer_relevant", run_id)
        rejected = {i["id"] for i in stale + invalid}
        items = [i for i in items if i["id"] not in rejected]
        items.sort(key=lambda i: i.get("published") or "", reverse=True)
        items = [i for i in items if i.get("mention")] + [i for i in items if not i.get("mention")]
        overflow = items[MAX_ITEMS:]
        items = items[:MAX_ITEMS]
        if overflow:
            log.warn("digest cap reached; overflow left pending", selected=len(items), pending=len(overflow), cap=MAX_ITEMS)
        log.info("gate", queued=len(items) - len([i for i in items if i.get("judge_why")]),
                 rescued=len([i for i in items if i.get("judge_why")]), total=len(items), stale=len(stale),
                 stale_judge=len(stale_candidates), invalid=len(invalid), overflow=len(overflow), cap=MAX_ITEMS)
        if not items:
            log.info("silent")
            _emit(SILENT)
            return

        prices = fetchers.prices(cfg)
        if a.raw:
            text = render(items, cfg, prices, "EVENING DIGEST — sector & market news touching your holdings")
            text += "\n\n## CLOSING PRICES\n" + (render_prices(cfg, prices) or "(price fetch failed)")
            print(text)
            return
        try:
            stories = digest_writer.run(cfg, items)
        except Exception as e:
            log.error("digest writer failed; falling back to one line per item", err=f"{type(e).__name__}: {e}")
            stories = _fallback(items)
        commit = not a.dry_run and not a.preview
        if not stories:
            log.info("silent", reason="writer kept nothing", items=len(items))
            _emit(SILENT)
        else:
            text = render_digest(cfg, items, stories, prices, datetime.now(fetchers.MYT))
            log.info("message", lines=text.count("\n") + 1, chars=len(text), items=len(items), stories=len(stories))
            _emit(text)
        if commit:
            memory.advance([i["id"] for i in items], "digested", run=run_id)


if __name__ == "__main__":
    main()
