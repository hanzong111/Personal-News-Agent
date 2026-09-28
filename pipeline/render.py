"""Turn matched items into the text block that gets injected into the Hermes agent prompt."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from .config import Config
from .fetchers import MYT


def _fmt_price(p: dict | None) -> str:
    if not p:
        return ""
    sign = "+" if p["pct"] >= 0 else ""
    return f" — RM{p['price']:.3f} ({sign}{p['pct']:.2f}% today)"


def _fmt_item(it: dict) -> str:
    try:
        dt = datetime.fromisoformat(it["published"]).astimezone(MYT)
        # Bursa filings carry a date only, so don't print a fake 00:00
        when = dt.strftime("%d %b %Y") if it.get("kind") == "announcement" else dt.strftime("%d %b %Y %H:%M")
    except Exception:
        when = "?"
    flag = " [HIGH]" if it.get("impact") == "high" else (" (mention only)" if it.get("mention") else "")
    line = f"- [{it['source']}] {when}{flag} — {it['title']}"
    if it.get("summary"):
        line += f"\n    {it['summary']}"
    if it.get("judge_why"):
        line += f"\n    (relevance: {it['judge_why']})"
    line += f"\n    {it['url']}"
    return line


def render(items: list[dict], cfg: Config, prices: dict, heading: str) -> str:
    """Group tier-1 items by holding, tier-2 by sector."""
    by_code: dict[str, list] = defaultdict(list)
    by_sector: dict[str, list] = defaultdict(list)
    macro: list = []
    for it in items:
        if it["codes"]:
            for c in it["codes"]:
                by_code[c].append(it)
        elif it["sectors"]:
            for s in it["sectors"]:
                by_sector[s].append(it)
        else:
            macro.append(it)

    out = [f"{heading} — {datetime.now(MYT).strftime('%a %d %b %Y %H:%M')} MYT", ""]
    for h in cfg.holdings:
        if h.code in by_code:
            out.append(f"## {h.label} ({h.code}) {h.name}{' [WATCHLIST — not held]' if h.watch else ''}{_fmt_price(prices.get(h.code))}")
            out.extend(_fmt_item(i) for i in by_code[h.code])
            out.append("")
    for key, its in by_sector.items():
        s = cfg.sectors[key]
        out.append(f"## SECTOR: {s.label} ({cfg.touches(key)})")
        out.extend(_fmt_item(i) for i in its)
        out.append("")
    if macro:
        out.append("## MACRO / MARKET (touches all holdings)")
        out.extend(_fmt_item(i) for i in macro)
        out.append("")
    return "\n".join(out).rstrip()


def render_prices(cfg: Config, prices: dict) -> str:
    rows = []
    for h in cfg.holdings:
        p = prices.get(h.code)
        if p:
            sign = "+" if p["pct"] >= 0 else ""
            rows.append(f"- {h.short} ({h.code}): RM{p['price']:.3f} ({sign}{p['pct']:.2f}%)")
    return "\n".join(rows)
