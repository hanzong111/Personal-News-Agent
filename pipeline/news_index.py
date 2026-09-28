"""Malaysia headline index — orchestrator.

  fetch (code) → CLASSIFIER (Haiku) → EDITOR (Sonnet) → RENDERER (code) → stdout

Run by Hermes cron in --no-agent mode: stdout IS the message; empty stdout = nothing sent.

  --cron            commit state (mark seen, save pool/index, advance last_run) and print the message
  --preview [--hours N] [--all]   build an index without committing (tests)
  --more SECTION    print the '+K more' list for a section of the last index (no LLM)
"""
from __future__ import annotations
import argparse
import json
import re
from agents import classifier, editor, renderer
from . import hold, news_fetch
from .log import get as _get_log, new_run

log = _get_log("news.index")


def dedup_en(stories: list[dict]) -> list[dict]:
    """Second dedup pass on the English titles: same story if term overlap >= 0.5. Keeps the copy
    with more reports (then newer) and adds the other's reports to it."""
    def terms(t):
        return {w for w in re.sub(r"[^a-z0-9 ]", " ", t.lower()).split() if len(w) > 3 and w not in news_fetch.STOP}
    kept: list[dict] = []
    for s in sorted(stories, key=lambda x: (x["reports"], x["time"]), reverse=True):
        ts = terms(s["title_en"])
        for k in kept:
            kt = terms(k["title_en"])
            if ts and kt and len(ts & kt) / len(ts | kt) >= 0.5 and k["section"] == s["section"]:
                k["reports"] += s["reports"]
                break
        else:
            kept.append(s)
    return sorted(kept, key=lambda x: x["time"], reverse=True)


def build(meta: dict, stories: list[dict]) -> tuple[str, dict]:
    labels = classifier.run([{"id": s["id"], "title": s["title"], "src": s["src"], "reports": s["reports"]} for s in stories])
    classified = []
    for s in stories:
        lab = labels[s["id"]]
        classified.append({"id": s["id"], "reports": s["reports"], "time": s["ts"].astimezone(news_fetch.MYT).isoformat(timespec="minutes"),
                           "title": s["title"], "url": s["url"], **lab})
    my = [c for c in classified if c["malaysian"]]
    malaysian = dedup_en(my)
    meta["dropped"] = len(classified) - len(malaysian)
    log.info("classified", stories=len(classified), malaysian=len(my), foreign_dropped=len(classified) - len(my),
             en_dedup_merged=len(my) - len(malaysian))
    plan = editor.run(malaysian)
    text = renderer.render_index({k: meta[k] for k in ("edition", "window", "dropped")}, malaysian, plan)
    shown = {i for t in plan["threads"] for i in t["developments"]} | {i for ids in plan["highlights"].values() for i in ids}
    thread_ids = {i for t in plan["threads"] for i in t["story_ids"]}
    record = {"meta": {k: meta[k] for k in ("edition", "window", "dropped", "reports")}, "stories": malaysian,
              "plan": plan, "shown": sorted(shown | thread_ids)}
    return text, record


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cron", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--hours", type=int, default=8)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--more", metavar="SECTION")
    a = ap.parse_args(argv)
    new_run("news")
    log.info("run start", mode="more" if a.more else "cron" if a.cron else "preview", hours=a.hours, limit=a.limit, include_seen=a.all)

    if a.more:
        rec = json.loads(news_fetch.INDEX.read_text(encoding="utf-8")) if news_fetch.INDEX.exists() else None
        print(renderer.render_more(a.more, rec["stories"], set(rec["shown"])) if rec else "no index yet")
        return

    meta, stories = news_fetch.collect(hours=a.hours, limit=a.limit, since_last=a.cron, include_seen=a.all)
    log.info("window", edition=meta["edition"], window=meta["window"], stories=len(stories), reports=meta["reports"],
             feeds_ok=f"{meta['feeds_ok']}/{meta['feeds']}")
    if not stories:
        log.info("silent")
        return                                   # empty stdout -> silent tick
    text, record = build(meta, stories)
    if a.cron:
        news_fetch.commit(meta, stories)
        news_fetch.INDEX.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("message", lines=text.count("\n") + 1, chars=len(text), committed=a.cron)
    hold.until_target("headlines")
    print(text)


if __name__ == "__main__":
    main()
