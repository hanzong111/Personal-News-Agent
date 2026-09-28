"""RENDERER — no LLM. Turns classified stories + the editor's plan into the exact message text.

Deterministic: same inputs -> same bytes. Layout rules live here, not in prompts.
"""
from __future__ import annotations
from collections import defaultdict
from pipeline.log import get as _get_log

log = _get_log("renderer")

SECTION_META = {
    "POLITICS": ("🏛️", "POLITICS"), "ECONOMY": ("💰", "ECONOMY"), "POLICY": ("📜", "POLICY"),
    "HAZE_WEATHER": ("🌫️", "HAZE & WEATHER"), "INCIDENTS": ("🚨", "INCIDENTS"),
    "SPORT": ("🏅", "SPORT"), "OTHER": ("📣", "OTHER"),
}
ORDER = ["POLITICS", "ECONOMY", "POLICY", "HAZE_WEATHER", "INCIDENTS", "SPORT", "OTHER"]
SENT = {"pos": "🟢", "neg": "🔴", "neu": "🟡", "routine": "➖"}
DIVIDER = "━━━━━━━━━━━━━━━"
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]


def _emoji(s: dict) -> str:
    e = SENT.get(s["sentiment"], "🟡")
    return f"⚠️{e}" if s.get("risk") else e


def _line(s: dict) -> str:
    tag = f" ({s['reports']})" if s.get("reports", 1) >= 2 else ""
    return f"{_emoji(s)} {s['title_en']}{tag}"


def render_index(meta: dict, stories: list[dict], plan: dict) -> str:
    """meta: {edition, window, dropped}. stories: classified Malaysian stories. plan: editor output."""
    by_id = {s["id"]: s for s in stories}
    by_sec: dict[str, list] = defaultdict(list)
    for s in stories:
        by_sec[s["section"]].append(s)
    in_thread = {i for t in plan["threads"] for i in t["story_ids"]}
    total_reports = lambda ids: sum(by_id[i].get("reports", 1) for i in ids if i in by_id)

    out = [f"🇲🇾 **MALAYSIA · {meta['edition']} · {meta['window']}**",
           f"{len(stories)} stories · {len(plan['threads'])} threads"]
    if plan["threads"]:
        out += ["", "🔥 **TOP THREADS**"]
        for n, t in enumerate(plan["threads"]):
            n_rep = total_reports(t['story_ids'])
            out.append(f"{NUMS[n]} **{t['name']}** ({n_rep} report{'s' if n_rep != 1 else ''})")
            for i in t["developments"]:
                if i in by_id:
                    out.append(f"  ↳ {_emoji(by_id[i])} {by_id[i]['title_en']}")
    collapsed = []
    for sec in ORDER:
        items = by_sec.get(sec, [])
        if not items:
            continue
        remaining = [s for s in items if s["id"] not in in_thread]
        if sec in plan["collapse"]:
            collapsed.append(f"{SECTION_META[sec][0]} {SECTION_META[sec][1]} · {len(remaining)}")
            continue
        shown = [by_id[i] for i in plan["highlights"].get(sec, []) if i in by_id]
        more = len(remaining) - len(shown)
        if not shown and more == 0:
            continue
        emo, name = SECTION_META[sec]
        head = f"{emo} **{name}**" + (f" · +{more} more" if more > 0 else "")
        out += ["", DIVIDER, head]
        out += [_line(s) for s in shown]
        if not shown:
            out.append(f"→ say \"more {name.lower()}\"")
    if collapsed:
        out += ["", " · ".join(collapsed) + " → say \"more sport\" / \"more other\""]
    out += ["", f"💬 \"more <section>\" · \"tell me about <headline>\" · {meta.get('dropped', 0)} foreign/irrelevant dropped"]
    text = "\n".join(out)
    log.info("index rendered", lines=len(out), chars=len(text), threads=len(plan["threads"]),
             sections_shown=len(plan["highlights"]), collapsed=len(collapsed))
    return text


def render_more(section: str, stories: list[dict], shown_ids: set[str], limit: int = 15) -> str:
    """The '+K more' list for one section: everything classified there that the index didn't show."""
    sec = section.upper().replace(" & ", "_").replace(" ", "_")
    sec = {"HAZE": "HAZE_WEATHER", "WEATHER": "HAZE_WEATHER"}.get(sec, sec)
    items = [s for s in stories if s["section"] == sec and s["id"] not in shown_ids]
    if sec not in SECTION_META:
        return f"Unknown section '{section}'. Sections: " + ", ".join(v[1].lower() for v in SECTION_META.values())
    emo, name = SECTION_META[sec]
    if not items:
        return f"{emo} {name}: nothing beyond what the index showed."
    out = [f"{emo} **{name}** · {len(items)} more"] + [_line(s) for s in items[:limit]]
    if len(items) > limit:
        out.append(f"+{len(items) - limit} more, ask again for the rest")
    return "\n".join(out)


# ---------------------------------------------------------------- Bursa alert
TYPE_EMOJI = {"earnings": "📊", "contract": "📝", "corporate-action": "💰", "analyst": "🔍",
              "regulation": "🏛️", "commodity": "🛢️", "macro": "🌏", "management": "👔", "other": "📣"}
SENT3 = {"pos": "🟢", "neg": "🔴", "neu": "🟡"}


def _trend(p: dict | None) -> str:
    if not p:
        return "➖"
    return "📈" if p["pct"] > 0 else "📉" if p["pct"] < 0 else "➖"


def render_alert(cfg, items: list[dict], verdicts: dict[str, dict], prices: dict) -> str:
    """items: tier-1 items (with 'id','codes','published','source','url'); verdicts: briefer output."""
    kept = [i for i in items if verdicts.get(i["id"], {}).get("keep")]
    if not kept:
        return ""
    order = {h.code: n for n, h in enumerate(cfg.holdings)}
    groups: dict[tuple, list] = {}                       # items naming the same set of holdings share one block
    for i in kept:
        groups.setdefault(tuple(sorted(i["codes"], key=lambda c: order.get(c, 99))), []).append(i)
    out = [f"📰 **Portfolio news** — {len(kept)} item{'s' if len(kept) != 1 else ''}"]
    for codes in sorted(groups, key=lambda cs: order.get(cs[0], 99)):
        out.append("")
        for c in codes:
            h = cfg.by_code(c)
            p = prices.get(c)
            out += [f"{_trend(p)} **{h.label if h else c}** ({c})", f"RM{p['price']:.3f} ({p['pct']:+.2f}%)" if p else "price n/a"]
        its = sorted(groups[codes], key=lambda i: (verdicts[i["id"]]["type"] not in ("earnings", "corporate-action", "contract"),
                                                    i.get("impact") != "high", i["published"]))
        for i in its:
            v = verdicts[i["id"]]
            emo = f"{TYPE_EMOJI.get(v['type'], TYPE_EMOJI['other'])} {'⚠️' if v['risk'] else ''}{SENT3[v['sentiment']]}"
            when = i["published"][:16].replace("T", " ")
            out += [f"{emo} **{v['headline']}**", f"🕒 {when}"]
            if v["summary"]:
                out.append(v["summary"])
            if v["why"]:
                out.append(f"💡 Why it matters: {v['why']}")
            out.append(f"🔗 [{source_label(i)}]({i['url']})")
    text = "\n".join(out)
    log.info("alert rendered", items=len(kept), blocks=len(groups), lines=len(out), chars=len(text))
    return text


# ---------------------------------------------------------------- Bursa evening digest
DIGEST_SECTION_EMOJI = {"solar": "☀️", "banking": "🏦", "construction": "🏗️", "healthcare": "🏥", "macro": "🌐",
                        "gloves": "🧤", "plantation": "🌴", "technology": "💾", "reit": "🏬", "property": "🏘️",
                        "oil_gas": "🛢️", "telco": "📡", "utilities": "⚡", "consumer": "🛒", "transport": "🚢",
                        "gaming": "🎰", "materials": "🏭"}

DIGEST_SENT = {"pos": "🟢", "neg": "🔴", "neu": "🟡", "risk": "⚠️"}
MAX_LINKS = 2


def _when(item: dict) -> str:
    """'23 Sep 2026 10:06' in MYT; Bursa filings carry a date only."""
    from datetime import datetime
    from pipeline.fetchers import MYT
    try:
        dt = datetime.fromisoformat(item["published"]).astimezone(MYT)
    except (KeyError, ValueError):
        return "?"
    return dt.strftime("%d %b %Y") if item.get("kind") == "announcement" else dt.strftime("%d %b %Y %H:%M")


def _join_times(items: list[dict]) -> str:
    """'23 Sep 2026 10:06/10:25' for a few same-day reports, else '21 Sep 19:09 → 23 Sep 10:25 (9 reports)'."""
    stamps = sorted({_when(i) for i in items}, key=lambda s: next((i["published"] for i in items if _when(i) == s), s))
    if len(stamps) <= 1:
        return stamps[0] if stamps else "?"
    same_day = len({s[:11] for s in stamps}) == 1 and all(len(s) > 11 for s in stamps)
    if same_day and len(stamps) <= 3:
        return stamps[0][:11] + " " + "/".join(s[12:] for s in stamps)
    short = lambda s: s[:6] + s[11:] if len(s) > 11 else s[:6]      # '21 Sep 19:09'
    return f"{short(stamps[0])} → {short(stamps[-1])} ({len(items)} reports)"


def source_label(item: dict) -> str:
    """Honest link text: 'TheEdge via KLSE Screener' when the URL is the aggregator's copy, not the publisher's."""
    src = item.get("source") or "source"
    return src.replace(" (via ", " via ").rstrip(")") if " (via " in src else src


def _english_first(items: list[dict]) -> list[dict]:
    ascii_share = lambda s: sum(c.isascii() for c in s) / max(len(s), 1)
    return sorted(items, key=lambda i: ascii_share(i.get("title") or "") < 0.8)   # stable: keeps writer order


def render_digest(cfg, items: list[dict], stories: list[dict], prices: dict, now) -> str:
    """items: digest items by id source of truth for url/time/source; stories: digest_writer output."""
    by_id = {i["id"]: i for i in items}
    out = [f"🌆 **Evening digest** — {now:%a %d %b %Y}", ""]
    for h in cfg.holdings:
        p = prices.get(h.code)
        out += [f"{_trend(p)} **{h.label}** ({h.code})", f"RM{p['price']:.3f} ({p['pct']:+.2f}%)" if p else "price n/a"]
    order = [k for k in cfg.sectors if cfg.holdings_in_sector(k)] + ["macro"]
    placed = lambda s: s["section"] if s["section"] in order else "macro"   # unheld sector -> macro, never dropped
    for section in order:
        block = [s for s in stories if placed(s) == section]
        if not block:
            continue
        if section == "macro":
            head = f"{DIGEST_SECTION_EMOJI['macro']} **Macro / market** (touches all holdings)"
        else:
            head = f"{DIGEST_SECTION_EMOJI.get(section, '📌')} **{cfg.sectors[section].label}** ({cfg.touches(section)})"
        out += ["", head]
        for s in block:
            its = [by_id[i] for i in s["item_ids"] if i in by_id]
            out += ["", f"{DIGEST_SENT[s['sentiment']]} **{s['headline']}**", f"🕒 {_join_times(its)}"]
            if s["summary"]:
                out.append(s["summary"])
            if s["touches"]:
                out.append(f"🎯 Touches: {s['touches']}")
            links, seen = [], set()
            for i in _english_first(its):               # url + label straight from the stored item
                if i.get("url") and i["url"] not in seen and len(links) < MAX_LINKS:
                    seen.add(i["url"])
                    links.append(f"[{source_label(i)}]({i['url']})")
            if links:
                out.append("🔗 " + " · ".join(links))
    text = "\n".join(out)
    log.info("digest rendered", stories=len(stories), items=sum(len(s["item_ids"]) for s in stories),
             lines=len(out), chars=len(text))
    return text
