"""Experiment 3 — categorising news: Jev's typed answers vs the labels the original workflow stored.

    --corpus general   the last Malaysia headline index (data/state/news/index.json). Baseline: the Haiku
                       classifier's section / sentiment / risk, parsed out of a 60-row JSON reply.
    --corpus bursa     stock and sector items in news.db. Baseline: the story type from the Sonnet briefer
                       (alerts) or the curator (digests), and the briefer's sentiment; plus the keyword rules in
                       `curate._rule_type` as a third opinion.

Jev gets the same category definitions the prompts use, one request per item with all questions fanned out.
Shadow mode, read-only.

    python benchmarks/jev/categorize.py --corpus general
    python benchmarks/jev/categorize.py --corpus bursa
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import _common  # noqa: F401  (sets sys.path)
from _common import RESULTS, Jev, estimate
from pipeline import config, news_fetch
from pipeline.curate import _rule_type
from typesafe_sdk import Choice, Noul

# ---- general headlines: the classifier's own definitions (agents/classifier.py) ----------------------------
SECTION = Choice(
    instructions="Which section of a Malaysian news digest does `headline` belong in?",
    criteria={
        "POLITICS": "Parties, MPs, coalitions, pardons, elections, political figures and their statements.",
        "ECONOMY": "Markets, companies, the ringgit, inflation, trade, prices, jobs, business results.",
        "POLICY": "Government programmes, ministries, budget measures, regulations, diplomacy.",
        "HAZE_WEATHER": "Haze and air-quality readings, floods, rain, storms, cloud seeding.",
        "INCIDENTS": "Crime, accidents, fires, scams, rescues, court cases about crimes.",
        "SPORT": "Sport of any kind.",
        "OTHER": "Culture, lifestyle, tech and gadgets, tourism, obituaries, human interest.",
    },
)
SENTIMENT_GEN = Choice(
    instructions="What is the tone of the news in `headline` for Malaysia and Malaysians?",
    criteria={
        "pos": "Good news: progress, gains, relief, wins, help arriving.",
        "neg": "Bad news: harm, losses, deaths, conflict, rising costs, setbacks.",
        "neu": "Mixed or neutral: facts without a clear good or bad direction.",
        "routine": "Scheduled, expected or no-change news, such as forecasts of a stable range, routine "
                   "announcements or obituaries.",
    },
)
RISK = Noul(
    instructions="Does `headline` signal a material risk to people, the economy or the government?",
    criteria={"true": "Deaths or injuries, unhealthy air, fiscal stress, market turmoil, coalition instability, "
                      "a threat to public safety.",
              "false": "Ordinary news without a material risk."},
)

# ---- Bursa items: the briefer's / curator's taxonomy (agents/briefer.py, agents/curator.py) ----------------
TYPE = Choice(
    instructions="What kind of stock-market news is `item`?",
    criteria={
        "earnings": "Quarterly or annual results, profit, revenue, guidance.",
        "contract": "A contract, tender, order or project won, lost or awarded.",
        "corporate-action": "Dividends, rights issues, placements, buybacks, mergers, acquisitions, "
                            "disposals, listings, IPOs, share splits.",
        "analyst": "A research house or analyst's rating, target price, recommendation or outlook.",
        "regulation": "A law, rule, policy, tariff or regulator's decision affecting companies.",
        "commodity": "Commodity prices or supply: oil, gas, palm oil, metals, electricity.",
        "macro": "The economy, interest rates, the ringgit, the market or index as a whole, the budget.",
        "management": "Directors, the CEO or chairman, appointments, resignations, boardroom changes.",
        "other": "None of the above.",
    },
)
SENTIMENT_STOCK = Choice(
    instructions="For a shareholder of `company`, is the news in `item` good, bad or neither?",
    criteria={"pos": "Good for the shareholder.", "neg": "Bad for the shareholder.",
              "neu": "Neither clearly good nor bad, or mixed."},
)


def load_general() -> list[dict]:
    idx = json.loads(news_fetch.INDEX.read_text(encoding="utf-8"))
    return [{"id": s["id"], "title": s["title"], "title_en": s["title_en"],
             "base": {"section": s["section"], "sentiment": s["sentiment"], "risk": bool(s["risk"])}}
            for s in idx["stories"]]


def load_bursa() -> list[dict]:
    cfg = config.load()
    names = {h.code: h.name for h in cfg.holdings}
    db = sqlite3.connect(f"file:{config.NEWS_DB}?mode=ro", uri=True)
    rows = db.execute("select id, title, summary, codes, stage, type, sentiment from items "
                      "where type != '' and title != ''").fetchall()
    out = []
    for id_, title, summary, codes, stage, typ, sent in rows:
        codes = json.loads(codes)
        item = {"title": title, "summary": (summary or "")[:300], "codes": codes, "macro": False}
        out.append({"id": id_, "title": title, "summary": item["summary"], "stage": stage,
                    "company": ", ".join(names.get(c, c) for c in codes) or None,
                    "base": {"type": typ, "sentiment": sent or None, "rule_type": _rule_type(item)}})
    return out


def ask_general(jev: Jev, s: dict) -> dict:
    state = {"headline": s["title_en"], **({"original_headline": s["title"]} if s["title"] != s["title_en"] else {})}
    a = jev.ask(state, {"section": SECTION, "sentiment": SENTIMENT_GEN, "risk": RISK})
    return {"section": a["section"]["choice"], "section_conf": a["section"]["confidence"],
            "sentiment": a["sentiment"]["choice"], "sentiment_conf": a["sentiment"]["confidence"],
            "risk": a["risk"]["noul"] > 0.5, "risk_p": a["risk"]["noul"]}


def ask_bursa(jev: Jev, it: dict) -> dict:
    state = {"item": {"headline": it["title"], **({"summary": it["summary"]} if it["summary"] else {})}}
    qs = {"type": TYPE}
    if it["company"] and it["base"]["sentiment"]:
        state["company"] = it["company"]
        qs["sentiment"] = SENTIMENT_STOCK
    a = jev.ask(state, qs)
    out = {"type": a["type"]["choice"], "type_conf": a["type"]["confidence"]}
    if "sentiment" in a:
        out |= {"sentiment": a["sentiment"]["choice"], "sentiment_conf": a["sentiment"]["confidence"]}
    return out


def compare(rows: list[dict], field: str, base_key: str | None = None, label: str = "") -> None:
    base_key = base_key or field
    pairs = [(r["base"][base_key], r["jev"][field], r) for r in rows if r["base"].get(base_key) is not None and field in r["jev"]]
    if not pairs:
        return
    agree = sum(b == j for b, j, _ in pairs)
    print(f"\n{label or field}: Jev agrees with baseline on {agree}/{len(pairs)} ({agree / len(pairs):.0%})")
    for (b, j), n in Counter((b, j) for b, j, _ in pairs if b != j).most_common(8):
        print(f"    baseline {str(b):<16} Jev {str(j):<16} x{n}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", choices=["general", "bursa"], default="general")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=30, help="disagreements to list per field")
    a = ap.parse_args(argv)

    rows = load_general() if a.corpus == "general" else load_bursa()
    print(f"{a.corpus}: {len(rows)} items with baseline labels")
    if a.dry_run:
        est = [({"headline": r.get("title_en", r["title"])}, {"section": SECTION, "sentiment": SENTIMENT_GEN, "risk": RISK})
               if a.corpus == "general" else ({"item": {"headline": r["title"]}}, {"type": TYPE, "sentiment": SENTIMENT_STOCK})
               for r in rows]
        print("Estimate: " + estimate(est))
        return 0

    jev = Jev()
    fn = ask_general if a.corpus == "general" else ask_bursa
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r, ans in zip(rows, pool.map(lambda r: fn(jev, r), rows)):
            r["jev"] = ans

    if a.corpus == "general":
        compare(rows, "section", label="Section (Haiku classifier vs Jev)")
        compare(rows, "sentiment", label="Sentiment (Haiku classifier vs Jev)")
        compare(rows, "risk", label="Risk flag (Haiku classifier vs Jev)")
        fields = [("section", "section_conf"), ("sentiment", "sentiment_conf"), ("risk", "risk_p")]
    else:
        compare(rows, "type", label="Story type (Sonnet briefer / curator vs Jev)")
        compare(rows, "type", "rule_type", label="Story type (keyword rules vs Jev)")
        compare(rows, "sentiment", label="Sentiment for the shareholder (Sonnet briefer vs Jev)")
        fields = [("type", "type_conf"), ("sentiment", "sentiment_conf")]

    for f, conf in fields:
        dis = [r for r in rows if f in r["jev"] and r["base"].get(f) is not None and r["base"][f] != r["jev"][f]]
        if dis:
            print(f"\n--- {f} disagreements ({len(dis)}, showing {min(a.show, len(dis))}):")
            for r in sorted(dis, key=lambda r: -float(r["jev"][conf]))[:a.show]:
                extra = f" rule={r['base']['rule_type']}" if f == "type" and a.corpus == "bursa" else ""
                print(f"  base={str(r['base'][f]):<16} jev={str(r['jev'][f]):<16} {conf}={float(r['jev'][conf]):.2f}{extra}"
                      f"  {r.get('title_en', r['title'])[:95]}")

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"categorize-{a.corpus}-{datetime.now():%Y%m%d-%H%M}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"\n{jev.cost_line()}\nSaved {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
