"""Malaysian news candidate pool — deterministic, no LLM.

Pulls Google News RSS (EN + BM) and direct publisher feeds, dedups, clusters same-story reports,
windows since the previous index, and persists state under data/state/news/.

Library:  collect(...) -> (meta, stories)   commit(meta, stories)   pool(query)   article(ref)
CLI:      python -m pipeline.news_fetch --pool [kw] | --article <id|url> | --raw [--hours N]
"""
from __future__ import annotations
import argparse
import datetime as dt
import email.utils
import hashlib
import html as _html
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents import jev
from . import gnews_resolve
from .config import STATE
from .log import get as _get_log

log = _get_log("news.fetch")

NEWS_STATE = STATE / "news"
NEWS_STATE.mkdir(parents=True, exist_ok=True)
SEEN = NEWS_STATE / "seen.json"
LAST_RUN = NEWS_STATE / "last_run.json"
POOL = NEWS_STATE / "pool.jsonl"        # every story in the last index window (for follow-ups)
INDEX = NEWS_STATE / "index.json"       # last index: classified stories + editor plan + shown ids

MYT = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
CTX = ssl.create_default_context()

GOOGLE_QUERIES = [
    ("Malaysia", "en-MY", "MY:en"),
    ("Malaysia politics", "en-MY", "MY:en"),
    ("Malaysia economy OR Bursa OR ringgit", "en-MY", "MY:en"),
    ("Malaysia haze OR API OR banjir", "en-MY", "MY:en"),
    ("Malaysia police OR fire OR accident", "en-MY", "MY:en"),
    ("Malaysia berita", "ms-MY", "MY:ms"),
    ("Malaysia politik", "ms-MY", "MY:ms"),
]
DIRECT_FEEDS = [
    ("Malay Mail", "https://www.malaymail.com/feed/rss/malaysia"),
    ("Free Malaysia Today", "https://freemalaysiatoday.com/category/nation/feed/"),
    ("The Sun", "https://thesun.my/feed"),
    ("Scoop", "https://www.scoop.my/feed"),
    ("Malaysiakini", "https://www.malaysiakini.com/rss/my/news.rss"),
    ("CNA", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml"),
]
PRIORITY = re.compile(r"\b(resign|quit|sack|dissolve|coup|treason|impeach|step down|snap election|no-confidence|"
                      r"emergency|state of emergency|ends cooperation|cooperation ends|breaks? (?:up|away)|"
                      r"letak jawatan|bubar|darurat|berakhir)\b", re.I)
NOTABLE = re.compile(r"\b(arrest|charged|dead|killed|died|maut|crash|fire|kebakaran|flood|banjir|collapse|raid|"
                     r"verdict|ruling|pardon|fined|hike|slump|surge|ban|shut|strike|tangkap)\b", re.I)
NOISE = re.compile(r"(horoscope|recipe|TradingView|Return on invested|stock forecast|sponsored|advertorial|listicle|top \d+ things)", re.I)

# ---- story clustering: same story if >= 2 shared rare terms (df <= RARE_DF) or term overlap >= JACCARD
STOP = set("""a an the and or of to in on at for by with from into over after before about than that this
these those is are was were be been has have had will would could should may might not no yes its his her
their our your who what when where why how says said say tells told amid as up out off more most new latest
malaysia malaysian malaysians govt government minister datuk seri tan sri dr news today tonight yesterday
yang dan di ke dari untuk dengan pada dalam tidak akan sudah telah kata kerajaan menteri ini itu oleh bagi
adalah ialah juga lagi masih ada tak bukan
asian games asiad aichi nagoya 2026 medal medals gold silver bronze sports sport other match final
bursa klci shares stock stocks market markets ringgit""".split())
RARE_DF = 2
JACCARD = 0.45
JEV_MAX_PAIRS = 400     # Jev mode: cap on candidate pairs judged per run (most-overlapping first)
JEV_LOOSE = 0.15        # Jev mode: candidate if Jaccard >= this or any shared rare word


def _fetch(url: str, timeout: int = 20) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
    except Exception:
        return None


def _parse(raw: bytes, default_src: str = "") -> list[dict]:
    out = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return out
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        try:
            when = email.utils.parsedate_to_datetime((it.findtext("pubDate") or "").strip())
            when = when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if not title or not link:
            continue
        src = default_src
        if not src and " - " in title:
            title, src = title.rsplit(" - ", 1)
        out.append({"title": title.strip(), "url": link, "src": src.strip(), "ts": when})
    return out


def _norm(title: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    t = " ".join(w for w in t.split() if len(w) > 3)
    return " ".join(sorted(t.split())[:8])


def _terms(title: str) -> set[str]:
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    return {w for w in t.split() if len(w) > 3 and w not in STOP}


def cluster(items: list[dict]) -> list[dict]:
    n = len(items)
    tsets = [_terms(it["title"]) for it in items]
    df: dict[str, int] = {}
    for ts in tsets:
        for w in ts:
            df[w] = df.get(w, 0) + 1
    rare = [{w for w in ts if df[w] <= RARE_DF} for ts in tsets]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rule_pairs, loose = [], []
    for i in range(n):
        for j in range(i + 1, n):
            if tsets[i] and tsets[j]:
                shared_rare = len(rare[i] & rare[j])
                jac = len(tsets[i] & tsets[j]) / len(tsets[i] | tsets[j])
                rule = shared_rare >= 2 or jac >= JACCARD
                if rule:
                    rule_pairs.append((i, j))
                if rule or shared_rare >= 1 or jac >= JEV_LOOSE:
                    loose.append((i, j, rule, shared_rare + jac))
    links = rule_pairs
    if jev.enabled() and loose:
        # Jev decides each candidate pair; it also catches the same story told in another language, which
        # shares only names. Pairs it can't answer keep the rule's verdict.
        loose = sorted(loose, key=lambda p: -p[3])[:JEV_MAX_PAIRS]
        pair_items = [({"title": items[i]["title"], "src": items[i].get("src", "")},
                       {"title": items[j]["title"], "src": items[j].get("src", "")}) for i, j, _, _ in loose]
        scores = jev.same_event(pair_items, role="cluster")
        links = [(i, j) for (i, j, rule, _), s in zip(loose, scores) if (s > jev.SAME_AT if s is not None else rule)]
        log.info("jev clustering", candidates=len(loose), linked=len(links), rule_would_link=len(rule_pairs))
    for i, j in links:
        parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    out = []
    for members in groups.values():
        members.sort(key=lambda it: (it.get("reports", 1), it["ts"]), reverse=True)
        rep = members[0]
        rep["related"] = members[1:]
        rep["reports"] = sum(m.get("reports", 1) for m in members)
        out.append(rep)
    return out


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def collect(hours: int = 8, limit: int = 200, since_last: bool = True, include_seen: bool = False) -> tuple[dict, list[dict]]:
    """Fetch, dedup, window, cluster. Returns (meta, stories). Side-effect free."""
    now = dt.datetime.now(dt.timezone.utc)
    last = _load(LAST_RUN, {}).get("ts") if since_last else None
    if last:
        span = (now - dt.datetime.fromtimestamp(last, dt.timezone.utc)).total_seconds() / 3600 + 1   # 1h grace
        hours = int(min(max(span, 2), 24))
    days = max(1, (hours + 23) // 24)
    urls = [("", f"https://news.google.com/rss/search?q={urllib.parse.quote(f'{q} when:{days}d')}&hl={hl}&gl=MY&ceid={ceid}")
            for q, hl, ceid in GOOGLE_QUERIES] + DIRECT_FEEDS
    log.debug("feeds …", feeds=len(urls))
    with ThreadPoolExecutor(max_workers=10) as pool:
        raws = list(pool.map(lambda p: (p[0], _fetch(p[1])), urls))
    items, ok = [], 0
    for (src, url), (_, raw) in zip(urls, raws):
        got = _parse(raw, src) if raw else []
        ok += bool(got)
        items += got
        (log.debug if got else log.warn)("feed", source=src or "Google News", items=len(got), url=url[:70])
    log.info("feeds fetched", feeds_ok=ok, feeds=len(urls), items=len(items), window_h=hours, since_last=bool(last))

    cutoff = now - dt.timedelta(hours=hours)
    seen = _load(SEEN, {})
    best: dict[str, dict] = {}
    for it in items:
        if it["ts"] < cutoff or NOISE.search(it["title"]):
            continue
        key = _norm(it["title"])
        if not key:
            continue
        it["h"] = hashlib.sha1(key.encode()).hexdigest()[:12]
        it["dup"] = it["h"] in seen
        prev = best.get(key)
        if prev is None or it["ts"] > prev["ts"]:
            it["reports"] = (prev or {}).get("reports", 0) + 1
            best[key] = it
        else:
            prev["reports"] = prev.get("reports", 1) + 1

    fresh = sorted((i for i in best.values() if include_seen or not i["dup"]), key=lambda i: i["ts"], reverse=True)[:limit]
    raw_count = len(fresh)
    log.info("windowed", in_window=len(best), already_seen=sum(1 for i in best.values() if i["dup"]), fresh=raw_count, limit=limit)
    stories = sorted(cluster(fresh), key=lambda i: i["ts"], reverse=True)
    log.info("clustered", reports=raw_count, stories=len(stories), merged=raw_count - len(stories))
    gnews_resolve.apply(stories)
    for s in stories:
        s["id"] = s["h"][:6]
        s["flag"] = "TOP" if PRIORITY.search(s["title"]) else ("HOT" if NOTABLE.search(s["title"]) else "")
        for m in s["related"]:
            m["id"] = m["h"][:6]

    now_myt, start_myt = now.astimezone(MYT), cutoff.astimezone(MYT)
    edition = "Morning" if now_myt.hour < 12 else "Midday" if now_myt.hour < 18 else "Evening"
    meta = {"now": now, "edition": edition,
            "window": f"{start_myt.strftime('%a %H:%M')} → {now_myt.strftime('%H:%M')}",
            "hours": hours, "feeds_ok": ok, "feeds": len(urls), "reports": raw_count}
    return meta, stories


def commit(meta: dict, stories: list[dict]) -> None:
    """Mark everything in this window as sent; save the pool for follow-ups; advance last_run."""
    now = meta["now"]
    seen = _load(SEEN, {})
    cutoff = (now - dt.timedelta(days=4)).timestamp()
    seen = {k: v for k, v in seen.items() if v > cutoff}
    flat = [m for s in stories for m in [s] + s.get("related", [])]
    for it in flat:
        seen[it["h"]] = now.timestamp()
    SEEN.write_text(json.dumps(seen))
    with open(POOL, "w", encoding="utf-8") as fh:
        for it in flat:
            fh.write(json.dumps({"id": it["id"], "time": it["ts"].astimezone(MYT).isoformat(timespec="minutes"),
                                 "src": it["src"], "title": it["title"], "url": it["url"],
                                 "reports": it.get("reports", 1)}, ensure_ascii=False) + "\n")
    LAST_RUN.write_text(json.dumps({"ts": now.timestamp()}))
    log.info("committed", marked_seen=len(flat), pool=len(flat), seen_total=len(seen))


def pool(query: str = "") -> list[dict]:
    rows = [json.loads(l) for l in POOL.read_text(encoding="utf-8").splitlines() if l.strip()] if POOL.exists() else []
    q = query.lower()
    return [r for r in rows if not q or q in r["title"].lower() or q == r["id"]]


def article(ref: str) -> str:
    """Readable text of an article by pool id or URL (Chrome-impersonated fetch, <p> paragraphs only)."""
    url = ref if ref.startswith("http") else next((r["url"] for r in pool() if r["id"] == ref), None)
    if not url:
        return f"unknown id {ref}"
    try:
        from curl_cffi import requests as creq
        page = creq.get(url, impersonate="chrome", timeout=25).text
    except Exception:
        raw = _fetch(url)
        if not raw:
            return f"fetch failed: {url}"
        page = raw.decode("utf-8", "ignore")
    body = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|noscript).*?</\1>", " ", page)
    paras = [_html.unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)]
    text = "\n".join(p for p in paras if len(p) > 40)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    head = f"URL: {url}\n" + (f"TITLE: {_html.unescape(re.sub(r'<[^>]+>', '', title.group(1))).strip()}\n" if title else "")
    return head + (text[:6000] if text else "(no readable paragraphs found - open the URL)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", nargs="?", const="", metavar="KEYWORD")
    ap.add_argument("--article", metavar="ID_OR_URL")
    ap.add_argument("--raw", action="store_true", help="print the candidate table (debug)")
    ap.add_argument("--hours", type=int, default=8)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if a.pool is not None:
        for r in pool(a.pool):
            print(f"{r['id']} | {r['time'][:16].replace('T', ' ')} | {r['src'][:20]:20s} | {r['title']} | {r['url']}")
    elif a.article:
        print(article(a.article))
    elif a.raw:
        meta, stories = collect(hours=a.hours, since_last=False, include_seen=a.all)
        print(f"# {meta['edition']} {meta['window']} stories={len(stories)} reports={meta['reports']} feeds_ok={meta['feeds_ok']}/{meta['feeds']}")
        for s in stories:
            print(f"{s['id']} | {s['ts'].astimezone(MYT).strftime('%a %H:%M')} | {s['flag']:3s} | x{s['reports']:<2} | {s['src'][:20]:20s} | {s['title'][:100]}")
            for m in s["related"][:6]:
                print(f"   + {m['title'][:90]} ({m['src'][:18]})")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
