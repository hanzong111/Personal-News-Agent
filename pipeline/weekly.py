"""Weekly review — run by Hermes cron on Friday evening.

Prints, per holding: the week's price path (Yahoo daily OHLCV), change vs FBM KLCI, volume vs
its 4-week average, biggest daily move, plus the week's news (tier 1) and sector news (tier 2).
The agent turns it into an analysis with suggestions and cautions. Always wakes the agent.

  --days N   trading-week window in calendar days (default 7)
"""
from __future__ import annotations
import argparse
from datetime import date, timedelta
from . import config, fetchers, hold
from .memory import Memory
from .log import get as _get_log, new_run

log = _get_log("weekly")

MAX_SECTOR_ITEMS = 20
MAX_WATCH_NEWS = 3


def collapse_stories(items: list[dict]) -> list[dict]:
    """Keep the richest article for each curated story (or item id before curation)."""
    best: dict[str, dict] = {}
    for item in items:
        key = item.get("story_id") or item["id"]
        score = (bool(item.get("url")), bool(item.get("source")), len(item.get("summary") or ""))
        current = best.get(key)
        current_score = ((bool(current.get("url")), bool(current.get("source")), len(current.get("summary") or ""))
                         if current else (-1, -1, -1))
        if score > current_score:
            best[key] = item
    return list(best.values())


def week_stats(rows: list[dict], start: date) -> dict | None:
    wk = [r for r in rows if r["date"] >= start]
    prior = [r for r in rows if r["date"] < start]
    if not wk or not prior:
        return None
    prev_close = prior[-1]["close"]
    close = wk[-1]["close"]
    hi = max(r["high"] for r in wk); lo = min(r["low"] for r in wk)
    hi_day = max(wk, key=lambda r: r["high"])["date"]; lo_day = min(wk, key=lambda r: r["low"])["date"]
    vol = sum(r["volume"] for r in wk) / len(wk)
    base = prior[-20:]
    vol_base = (sum(r["volume"] for r in base) / len(base)) if base else 0
    moves = []
    pc = prev_close
    for r in wk:
        moves.append((r["date"], (r["close"] - pc) / pc * 100 if pc else 0.0, r["close"]))
        pc = r["close"]
    big = max(moves, key=lambda m: abs(m[1]))
    return {"prev_close": prev_close, "close": close, "pct": (close - prev_close) / prev_close * 100,
            "high": hi, "hi_day": hi_day, "low": lo, "lo_day": lo_day,
            "vol_ratio": (vol / vol_base) if vol_base else None, "big": big, "path": moves}


def _pct(x: float) -> str:
    return f"{x:+.2f}%"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    new_run("weekly")
    log.info("run start", days=a.days)
    cfg = config.load()
    config.LOOKBACK_HOURS = a.days * 24
    if not cfg.holdings:
        log.info("no stocks configured")
        print("No stocks configured yet — tell the user to message /setup to choose their stocks. Nothing else to review.")
        return

    today = date.today()
    start = today - timedelta(days=a.days)
    out = [f"WEEKLY REVIEW — {start.strftime('%d %b')} to {today.strftime('%d %b %Y')} (generated {today.strftime('%a %d %b')})", ""]

    klci = week_stats(fetchers.history("^KLSE"), start)
    if klci:
        out.append(f"## BENCHMARK FBM KLCI: {klci['prev_close']:,.2f} → {klci['close']:,.2f} ({_pct(klci['pct'])} on the week)")
        out.append("")

    with Memory() as memory:
        items = memory.delivered(a.days)
    by_code = {h.code: collapse_stories([i for i in items if h.code in i["codes"]]) for h in cfg.holdings}

    for h in cfg.owned:
        st = week_stats(fetchers.history(f"{h.code}.KL"), start)
        out.append(f"## {h.short} ({h.code}) {h.name}")
        if st:
            rel = f", {st['pct'] - klci['pct']:+.2f}pp vs KLCI" if klci else ""
            out.append(f"week: RM{st['prev_close']:.3f} → RM{st['close']:.3f} ({_pct(st['pct'])}{rel})")
            out.append(f"high RM{st['high']:.3f} ({st['hi_day'].strftime('%a')}) · low RM{st['low']:.3f} ({st['lo_day'].strftime('%a')})"
                       + (f" · volume {st['vol_ratio']:.1f}x its 4-week average" if st['vol_ratio'] else ""))
            out.append("daily: " + "  ".join(f"{d.strftime('%a')} RM{c:.3f} ({_pct(p)})" for d, p, c in st["path"]))
            out.append(f"biggest move: {st['big'][0].strftime('%a %d %b')} {_pct(st['big'][1])}")
        else:
            out.append("(price history unavailable)")
        news = sorted(by_code[h.code], key=lambda i: i["published"], reverse=True)
        out.append(f"news this week ({len(news)}):" if news else "news this week: none found")
        for i in news:
            flag = " [HIGH]" if i.get("impact") == "high" else ""
            out.append(f"- [{i.get('source') or 'memory'}] {i['published'][:16].replace('T', ' ')}{flag} — {i['title']}")
            if i.get("summary"):
                out.append(f"    {i['summary'][:300]}")
            if i.get("url"):
                out.append(f"    {i['url']}")
        out.append("")

    if cfg.watched:
        out.append("## WATCHLIST (not held — the user is watching these as possible buys)")
        for h in cfg.watched:
            st = week_stats(fetchers.history(f"{h.code}.KL"), start)
            rel = f", {st['pct'] - klci['pct']:+.2f}pp vs KLCI" if st and klci else ""
            price = f"RM{st['prev_close']:.3f} → RM{st['close']:.3f} ({_pct(st['pct'])}{rel})" if st else "price n/a"
            news = sorted(by_code[h.code], key=lambda i: i["published"], reverse=True)
            out.append(f"- {h.short} ({h.code}) {h.name}: {price}; news this week: {len(news)}")
            for i in news[:MAX_WATCH_NEWS]:
                flag = " [HIGH]" if i.get("impact") == "high" else ""
                out.append(f"    {i['published'][:10]}{flag} — {i['title']}" + (f" {i['url']}" if i.get("url") else ""))
        out.append("")

    sector_items = sorted(collapse_stories([i for i in items if i["stage"] == "digested" and i["sectors"]]),
                          key=lambda i: i["published"], reverse=True)
    if sector_items:
        out.append(f"## SECTOR / MACRO NEWS THIS WEEK (top {min(len(sector_items), MAX_SECTOR_ITEMS)} of {len(sector_items)})")
        for i in sector_items[:MAX_SECTOR_ITEMS]:
            secs = ", ".join(cfg.sectors[s].label for s in i["sectors"])
            out.append(f"- ({secs}) [{i.get('source') or 'memory'}] {i['published'][:10]} — {i['title']}")
            if i.get("url"):
                out.append(f"    {i['url']}")
    text = "\n".join(out).rstrip()
    log.info("message", lines=text.count("\n") + 1, chars=len(text), holdings=len(cfg.owned), watchlist=len(cfg.watched),
             holding_news=sum(len(v) for v in by_code.values()), sector_news=len(sector_items), klci=bool(klci))
    hold.until_target("weekly")
    print(text)


if __name__ == "__main__":
    main()
