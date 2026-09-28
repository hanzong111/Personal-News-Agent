"""Experiment 1 — is the stock the SUBJECT of the headline, or only quoted?

Today `pipeline.match.attribution_only` decides this with four regexes ("CIMB sees…", "— RHB"). Here the
alias match still finds the candidates, and Jev picks the company's role with one Choice per
(headline, stock). Runs in shadow mode over headlines already in data/state/news.db (read-only) and
compares the two verdicts. Nothing in the live pipeline changes.

    python benchmarks/jev/subject_check.py --dry-run          # candidates, sample request, cost estimate
    python benchmarks/jev/subject_check.py --days 30          # ask Jev, print agreement + disagreements
    python benchmarks/jev/subject_check.py --show-all         # also list every agreeing headline
    python benchmarks/jev/subject_check.py --also "1023:CIMB:CIMB Group Holdings Bhd:CIMB ,CIMB Securities" \
        --also "1066:RHBBANK:RHB Bank Bhd:RHB "                 # add stocks you don't hold (brokers are the
                                                                # interesting case: they're quoted all the time)

Results go to benchmarks/jev/results/ (gitignored: they contain your stocks and headlines).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import _common  # noqa: F401  (sets sys.path)
from _common import RESULTS, Jev, estimate
from pipeline import config
from pipeline.match import _alias_hit, attribution_only
from typesafe_sdk import Choice

ROLE = Choice(
    instructions="In `headline`, what role does the listed company described in `company` play?",
    criteria={
        "subject": "The headline reports news about this company itself: its results, contracts, deals, "
                   "share price or trading, management, lawsuits, regulatory action, plans or products.",
        "source_only": "The company appears only as the source of an opinion, forecast, trading call or "
                       "research about something else: its securities, research or investment-bank arm "
                       "commenting on the market, the economy, a commodity, an index, a sector or a different "
                       "company. Includes analyst and trading calls such as 'X stays short', 'X turns "
                       "bearish on gold', 'X keeps buy call on Y' or 'Y upgraded by X'.",
        "passing_mention": "The company is one of several names listed, or is mentioned in passing, and the "
                           "headline is mainly about something else.",
        "different_entity": "The matching name refers to something other than this listed company: a person, "
                            "a place, a product, or a different organisation with a similar name.",
    },
)
LOW_CONFIDENCE = 0.7          # starting point to review, not a tuned threshold


def extra_stock(spec: str) -> config.Holding:
    """'code:SHORT:Full Name:alias1,alias2' -> a Holding used only by this experiment."""
    code, short, name, aliases = (spec.split(":", 3) + [""])[:4]
    return config.Holding(code=code, short=short, name=name, sector="other",
                          aliases=[x for x in aliases.split(",") if x.strip()] or [name])


def _rows(days: int, corpus: str) -> list[tuple]:
    """(key, title, source, published) — bursa: news.db items; general: the Malaysia headline pool."""
    if corpus == "general":
        from pipeline import news_fetch
        return [(r["title"].lower(), r["title"], r.get("src", ""), r.get("time", "")) for r in news_fetch.pool()]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db = sqlite3.connect(f"file:{config.NEWS_DB}?mode=ro", uri=True)
    return db.execute("select tkey, title, source, published from items where title != '' and fetched_at >= ? "
                      "order by published desc", (since,)).fetchall()


def candidates(days: int, extra: list[config.Holding], corpus: str = "bursa") -> list[dict]:
    """(headline, stock) pairs where an alias hits the title — the same recall step the pipeline uses."""
    cfg = config.load()
    stocks = cfg.holdings + extra
    rows = _rows(days, corpus)
    seen, out = set(), []
    for tkey, title, source, published in rows:
        if tkey in seen:
            continue
        seen.add(tkey)
        for h in stocks:
            if _alias_hit(title, h.aliases):
                out.append({"title": title, "source": source, "published": published[:16],
                            "code": h.code, "short": h.short, "name": h.name,
                            "aliases": [a.strip() for a in h.aliases],
                            "regex": "source_only" if attribution_only(title, h.aliases) else "subject"})
    return out


def state_for(c: dict) -> dict:
    return {"headline": c["title"],
            "company": {"name": c["name"], "ticker": c["short"], "also_written_as": c["aliases"]}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=30, help="look back this many days (default 30)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N pairs (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="no API calls: counts, one sample request, cost estimate")
    ap.add_argument("--show-all", action="store_true", help="list agreeing pairs too")
    ap.add_argument("--corpus", choices=["bursa", "general"], default="bursa",
                    help="bursa = stored stock news (default); general = Malaysia headline pool (unseen test set)")
    ap.add_argument("--also", action="append", default=[], metavar="CODE:SHORT:NAME:ALIASES",
                    help="test a stock you don't hold (repeatable); aliases comma-separated, trailing space = whole word")
    a = ap.parse_args(argv)

    pairs = candidates(a.days, [extra_stock(s) for s in a.also], a.corpus)
    if a.limit:
        pairs = pairs[:a.limit]
    print(f"{len(pairs)} (headline, stock) pairs from the last {a.days} days "
          f"({sum(p['regex'] == 'subject' for p in pairs)} alert under today's regex rule)")
    if not pairs:
        return 0
    if a.dry_run:
        print("\nSample request:\n" + json.dumps({"state": state_for(pairs[0]), "questions": {"role": ROLE.model_dump()}},
                                               indent=2, ensure_ascii=False))
        print("\nEstimate: " + estimate([(state_for(p), {"role": ROLE}) for p in pairs]))
        return 0

    jev = Jev()
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda p: jev.ask(state_for(p), {"role": ROLE})["role"], pairs))
    for p, ans in zip(pairs, answers):
        p["jev"], p["confidence"], p["probabilities"] = ans["choice"], ans["confidence"], ans["probabilities"]
        p["agree"] = (p["jev"] == "subject") == (p["regex"] == "subject")      # both alert, or both don't

    agree = sum(p["agree"] for p in pairs)
    print(f"\nAgreement on 'should this alert': {agree}/{len(pairs)} ({agree / len(pairs):.0%})")
    table: dict[tuple[str, str], int] = {}
    for p in pairs:
        table[(p["regex"], p["jev"])] = table.get((p["regex"], p["jev"]), 0) + 1
    print("\n  regex says     Jev says           pairs")
    for (r, j), n in sorted(table.items(), key=lambda kv: -kv[1]):
        print(f"  {r:<14} {j:<18} {n:>5}")

    def show(title: str, rows: list[dict]) -> None:
        if rows:
            print(f"\n{title} ({len(rows)})")
            for p in rows:
                print(f"  [{p['short']}] regex={p['regex']:<11} jev={p['jev']:<16} conf={p['confidence']:.2f}  {p['title'][:110]}")

    show("Disagreements — review these by hand", sorted((p for p in pairs if not p["agree"]), key=lambda p: -p["confidence"]))
    show(f"Jev unsure (confidence < {LOW_CONFIDENCE})", [p for p in pairs if p["agree"] and p["confidence"] < LOW_CONFIDENCE])
    if a.show_all:
        show("Agreements", [p for p in pairs if p["agree"]])

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"subject_check-{datetime.now():%Y%m%d-%H%M}.jsonl"
    out.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pairs))
    print(f"\n{jev.cost_line()}\nSaved {out.relative_to(RESULTS.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
