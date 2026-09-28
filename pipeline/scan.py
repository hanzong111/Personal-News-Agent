"""Alert scan — fetch, match, persist, brief and emit portfolio news.

Pending rows are never removed before success. A crash during the briefer, prices, renderer or
stdout leaves tier-1 items at stage ``alert`` for the next scan. Tier-2 and tier-0 items stay at
``digest`` and ``judge`` until the evening job processes them.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, fetchers, gnews_resolve, hold
from .match import tag, is_fresh, dedup_titles, is_repeat
from .memory import Memory
from .render import render, render_prices
from agents import briefer
from agents.renderer import render_alert
from .log import get as _get_log, new_run

log = _get_log("scan")
SILENT = json.dumps({"wakeAgent": False})


def collect(cfg: config.Config) -> list[dict]:
    items: list[dict] = []
    with fetchers.log.span("collect", holdings=len(cfg.holdings)):
        for h in cfg.holdings:
            items += fetchers.klse_news(h)
            items += fetchers.bursa_announcements(h, cfg)
            for q in h.queries:
                items += fetchers.gnews(q, codes=[h.code], sectors=[h.sector])
        for key, sector in cfg.sectors.items():
            if cfg.holdings_in_sector(key):
                for q in sector.queries:
                    items += fetchers.gnews(q, codes=[], sectors=[key])
        for q in cfg.macro_queries:
            for item in fetchers.gnews(q, codes=[], sectors=[]):
                item["macro"] = True
                items.append(item)
        items += fetchers.firehose()
    uniq: dict[str, dict] = {}
    for item in items:
        uniq.setdefault(item["id"], item)
    out = dedup_titles([tag(item, cfg) for item in uniq.values() if is_fresh(item)])
    return gnews_resolve.apply(out)


def _emit(text: str) -> None:
    if text != SILENT:                          # silent ticks have no message to time
        hold.until_target()
    sys.stdout.write(text.rstrip() + "\n")
    sys.stdout.flush()


def _fallback(items: list[dict]) -> dict[str, dict]:
    return {i["id"]: {"keep": True, "type": "other", "sentiment": "neu", "risk": False,
                      "headline": i["title"][:90], "summary": (i.get("summary") or "")[:400],
                      "why": "", "skip_reason": ""} for i in items}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-recent", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--raw", action="store_true", help="print the raw item list instead of running the briefer")
    a = ap.parse_args(argv)
    run_id = new_run("scan")
    log.info("run start", mode="status" if a.status else "bootstrap" if a.bootstrap else "dry-run" if a.dry_run else
             f"force-recent={a.force_recent}" if a.force_recent else "cron")
    cfg = config.load()
    log.info("portfolio loaded", holdings=len(cfg.holdings), sectors=len(cfg.sectors))
    if a.status:
        print(f"Holdings ({len(cfg.holdings)}):")
        for h in cfg.holdings:
            print(f"- {h.short} ({h.code}) {h.name} — sector: {cfg.sectors[h.sector].label}")
        print("\nPrices now:")
        print(render_prices(cfg, fetchers.prices(cfg)))
        return

    if not cfg.holdings:
        # Nothing to screen yet. Don't touch memory either: the first scan after setup must be the
        # bootstrap, or every story about the new stocks would alert at once.
        log.info("no stocks configured — run `python -m pipeline.setup` or /setup in chat")
        log.info("silent")
        _emit(SILENT)
        return

    with Memory() as memory:
        bootstrap = a.bootstrap or (memory.is_empty() and not a.dry_run and not a.force_recent)
        items = collect(cfg)
        if bootstrap:
            memory.add_many(((item, "seen") for item in items), run=run_id)
            log.info("bootstrap: marked as seen", items=len(items))
            log.info("silent")
            _emit(SILENT)
            return

        new = [item for item in items if not memory.known(item["id"], item.get("tkey"))]
        queued = [item for item in new if item["tier"] == 2]
        unmatched = [item for item in new if item["tier"] == 0]
        if a.force_recent:
            alerts = sorted((i for i in items if i["tier"] == 1), key=lambda i: i["published"], reverse=True)[:a.force_recent]
            queued, unmatched = [], []
        elif a.dry_run:
            alerts = [item for item in new if item["tier"] == 1]
        else:
            memory.add_many(((item, {1: "alert", 2: "digest"}.get(item["tier"], "judge")) for item in new), run=run_id)
            # The writer expires what it wrote: the same retention rules bursa-curate applies, run here on
            # every scan so the store stays bounded even if the 19:00 job never fires. Milliseconds.
            pruned = memory.prune()
            freed = sum(v for k, v in pruned.items() if isinstance(v, int) and not isinstance(v, bool))
            (log.info if freed else log.debug)("prune", **{k: v for k, v in pruned.items() if k != "at"})
            alerts = memory.pending("alert")

        recent = memory.recent_alerts()
        kept_alerts, repeat_ids, stale_ids, sold_ids = [], [], [], []
        held = {h.code for h in cfg.holdings}
        for item in alerts:
            if not is_fresh(item):
                stale_ids.append(item["id"])
                continue
            if not set(item.get("codes") or []) & held:
                sold_ids.append(item["id"])
                continue
            rep = is_repeat(item, recent)
            if rep:
                repeat_ids.append(item["id"])
                log.info("repeat of recent alert, skipped", codes=",".join(item["codes"]),
                         title=item["title"][:70], earlier=rep["title"][:60])
            else:
                kept_alerts.append(item)
        alerts = kept_alerts
        if not a.dry_run and not a.force_recent:
            memory.advance(repeat_ids, "dropped", "repeat_recent", run_id)
            memory.advance(stale_ids, "dropped", "stale", run_id)
            memory.advance(sold_ids, "dropped", "no_longer_held", run_id)

        log.info("gate", fetched=len(items), new=len(new), alerts=len(alerts), queued_for_digest=len(queued),
                 queued_for_judge=len(unmatched), pending_alerts=len(alerts),
                 committed=not a.dry_run and not a.force_recent)
        for item in alerts:
            log.info("alert item", codes=",".join(item["codes"]), impact=item.get("impact"),
                     source=item["source"][:30], title=item["title"][:80])
        if not alerts:
            log.info("silent")
            _emit(SILENT)
            return

        if a.raw:
            prices = fetchers.prices(cfg)
            text = render(alerts, cfg, prices, "NEW NEWS ON YOUR HOLDINGS")
            log.info("message", lines=text.count("\n") + 1, chars=len(text), items=len(alerts), mode="raw")
            _emit(text)
            if not a.dry_run and not a.force_recent:
                verdicts = _fallback(alerts)
                memory.save_verdicts(verdicts)
                memory.advance([i["id"] for i in alerts], "alerted", run=run_id)
            return

        verdicts = {i["id"]: i["_verdict"] for i in alerts if i.get("_verdict")}
        need_brief = [i for i in alerts if i["id"] not in verdicts]
        codes = {c for item in alerts for c in item.get("codes") or []}
        sectors = {s for item in alerts for s in item.get("sectors") or []}
        if need_brief:
            try:
                verdicts.update(briefer.run(cfg, need_brief, memory.notes_for(codes, sectors)))
            except Exception as e:
                log.error("briefer failed; falling back to raw list", err=f"{type(e).__name__}: {e}")
                verdicts.update(_fallback(need_brief))
        if not a.dry_run and not a.force_recent:
            memory.save_verdicts(verdicts)
        skipped = [i for i in alerts if not verdicts[i["id"]]["keep"]]
        kept = [i for i in alerts if verdicts[i["id"]]["keep"]]
        if not a.dry_run and not a.force_recent:
            for item in skipped:
                memory.advance([item["id"]], "dropped", verdicts[item["id"]].get("skip_reason") or "briefer_skipped", run_id)
        if not kept:
            log.info("silent", reason="briefer kept nothing", skipped=len(alerts))
            _emit(SILENT)
            return
        prices = fetchers.prices(cfg)
        text = render_alert(cfg, alerts, verdicts, prices)
        if queued:
            text += f"\n\n({len(queued)} sector/market item(s) queued for the evening digest — not shown here)"
        log.info("message", lines=text.count("\n") + 1, chars=len(text), items=len(kept), skipped=len(skipped))
        _emit(text)
        if not a.dry_run and not a.force_recent:
            memory.advance([i["id"] for i in kept], "alerted", run=run_id)


if __name__ == "__main__":
    main()
