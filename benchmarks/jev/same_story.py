"""Experiment 2 — do two headlines report the SAME news event?

Today four word-overlap rules decide this: `news_fetch.cluster` (shared rare words or Jaccard >= 0.45,
with a hand-tuned stopword list), `news_index.dedup_en`, `curate._rule_story` and `match.is_repeat`
(Jaccard >= 0.5). Here a loose code filter proposes candidate pairs and Jev rates each pair on a
three-level Score, as in TypeSafe's entity-alignment cookbook:

    0 different events   1 related (follow-up, reaction, analysis)   2 same event

Shadow mode, read-only. Two corpora:

    --corpus bursa     stock/sector items in data/state/news.db (what alerts and digests are built from)
    --corpus general   the Malaysia headline pool, data/state/news/pool.jsonl

    python benchmarks/jev/same_story.py --corpus bursa --dry-run
    python benchmarks/jev/same_story.py --corpus bursa
    python benchmarks/jev/same_story.py --corpus general --hours 48
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import _common  # noqa: F401  (sets sys.path)
from _common import RESULTS, Jev, estimate
from pipeline import config, news_fetch
from typesafe_sdk import Score

SAME = Score(
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
SAME_AT = 1.5            # expected score above this = same event (between 'related' and 'same')


def _terms(title: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", title.lower()).split() if len(w) > 3}


def load_bursa(days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db = sqlite3.connect(f"file:{config.NEWS_DB}?mode=ro", uri=True)
    rows = db.execute("select id, tkey, title, summary, source, published, codes, sectors, stage from items "
                      "where title != '' and tier > 0 and fetched_at >= ? order by published", (since,)).fetchall()
    seen, out = set(), []
    for id_, tkey, title, summary, source, published, codes, sectors, stage in rows:
        if tkey in seen:                        # exact duplicates are already merged by title_key
            continue
        seen.add(tkey)
        out.append({"id": id_, "title": title, "summary": (summary or "")[:240], "source": source,
                    "published": published[:16], "codes": json.loads(codes), "sectors": json.loads(sectors),
                    "stage": stage})
    return out


def load_general(hours: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, seen = [], set()
    for line in news_fetch.POOL.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            when = datetime.fromisoformat(r["time"])
        except (ValueError, KeyError):
            continue
        key = news_fetch._norm(r.get("title", ""))
        if when < since or not key or key in seen:
            continue
        seen.add(key)
        out.append({"id": r.get("id") or key[:16], "title": r["title"], "summary": "", "source": r.get("src", ""),
                    "published": r["time"][:16], "codes": [], "sectors": [], "stage": ""})
    return out


def candidate_pairs(items: list[dict], corpus: str) -> list[tuple[int, int, bool]]:
    """Over-find pairs worth asking about, and record today's rule verdict for each.

    bursa:   same stock or sector, and any shared word (4+ letters); rule = Jaccard >= 0.5 (is_repeat / curate)
    general: any shared non-stopword; rule = news_fetch.cluster (>= 2 shared rare words or Jaccard >= 0.45)
    """
    if corpus == "general":
        tsets = [news_fetch._terms(it["title"]) for it in items]
        df: dict[str, int] = {}
        for ts in tsets:
            for w in ts:
                df[w] = df.get(w, 0) + 1
        rare = [{w for w in ts if df[w] <= news_fetch.RARE_DF} for ts in tsets]
    else:
        tsets = [_terms(it["title"]) for it in items]
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = tsets[i], tsets[j]
            if not a or not b or not (a & b):
                continue
            jac = len(a & b) / len(a | b)
            if corpus == "general":
                rule = len(rare[i] & rare[j]) >= 2 or jac >= news_fetch.JACCARD
                if not rule and jac < 0.15 and len(rare[i] & rare[j]) < 1:
                    continue
            else:
                scope = set(items[i]["codes"]) & set(items[j]["codes"]) or \
                    (not items[i]["codes"] and not items[j]["codes"] and set(items[i]["sectors"]) & set(items[j]["sectors"]))
                if not scope:
                    continue
                rule = jac >= 0.5
            out.append((i, j, rule))
    return out


def state_for(a: dict, b: dict) -> dict:
    side = lambda it: {"title": it["title"], "source": it["source"], **({"summary": it["summary"]} if it["summary"] else {})}
    return {"headline_a": side(a), "headline_b": side(b)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", choices=["bursa", "general"], default="bursa")
    ap.add_argument("--days", type=int, default=30, help="bursa: look back this many days")
    ap.add_argument("--hours", type=int, default=48, help="general: look back this many hours")
    ap.add_argument("--max-pairs", type=int, default=600, help="cap on pairs asked (most-overlapping first)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    items = load_bursa(a.days) if a.corpus == "bursa" else load_general(a.hours)
    pairs = candidate_pairs(items, a.corpus)
    overlap = lambda p: len(_terms(items[p[0]]["title"]) & _terms(items[p[1]]["title"]))
    pairs = sorted(pairs, key=lambda p: (-p[2], -overlap(p)))[:a.max_pairs]
    print(f"{a.corpus}: {len(items)} distinct headlines, {len(pairs)} candidate pairs "
          f"({sum(p[2] for p in pairs)} merged by today's rule)")
    if not pairs:
        return 0
    if a.dry_run:
        i, j, _ = pairs[0]
        print("\nSample request:\n" + json.dumps({"state": state_for(items[i], items[j]), "questions": {"same": SAME.model_dump()}},
                                               indent=2, ensure_ascii=False))
        print("\nEstimate: " + estimate([(state_for(items[i], items[j]), {"same": SAME}) for i, j, _ in pairs]))
        return 0

    jev = Jev()
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda p: jev.ask(state_for(items[p[0]], items[p[1]]), {"same": SAME})["same"], pairs))

    rows = []
    for (i, j, rule), ans in zip(pairs, answers):
        probs = ans["probabilities"]
        level = max(range(3), key=lambda k: probs.get(str(k), probs.get(k, 0)) if isinstance(probs, dict) else probs[k])
        rows.append({"a": items[i], "b": items[j], "rule_same": rule, "score": ans["score"], "level": level,
                     "confidence": ans["confidence"], "jev_same": ans["score"] > SAME_AT})

    both = sum(r["rule_same"] and r["jev_same"] for r in rows)
    print(f"\n  rule=same & Jev=same        {both:>4}")
    print(f"  rule=same & Jev=not same    {sum(r['rule_same'] and not r['jev_same'] for r in rows):>4}   <- rule merges different stories")
    print(f"  rule=different & Jev=same   {sum(not r['rule_same'] and r['jev_same'] for r in rows):>4}   <- rule misses a repeat")
    print(f"  rule=different & Jev=diff   {sum(not r['rule_same'] and not r['jev_same'] for r in rows):>4}")
    print(f"  Jev levels: different {sum(r['level'] == 0 for r in rows)}, related {sum(r['level'] == 1 for r in rows)}, "
          f"same {sum(r['level'] == 2 for r in rows)}")

    def show(title: str, sel: list[dict], n: int = 25) -> None:
        if sel:
            print(f"\n{title} ({len(sel)}, showing {min(n, len(sel))})")
            for r in sel[:n]:
                print(f"  score={r['score']:.2f} conf={r['confidence']:.2f}\n     A: {r['a']['title'][:110]}\n     B: {r['b']['title'][:110]}")

    show("Rule merges, Jev says different or only related",
         sorted((r for r in rows if r["rule_same"] and not r["jev_same"]), key=lambda r: r["score"]))
    show("Rule keeps apart, Jev says same event",
         sorted((r for r in rows if not r["rule_same"] and r["jev_same"]), key=lambda r: -r["score"]))
    if a.corpus == "bursa":
        rep = [r for r in rows if r["jev_same"] and r["a"]["stage"] == "alerted" and r["b"]["stage"] == "alerted"]
        show("Repeat alerts you received (both alerted, Jev says same event)", rep)

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"same_story-{a.corpus}-{datetime.now():%Y%m%d-%H%M}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"\n{jev.cost_line()}\nSaved {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
