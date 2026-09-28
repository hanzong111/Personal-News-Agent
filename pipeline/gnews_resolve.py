"""Resolve Google News RSS redirect URLs to the real publisher URL.

Google News RSS links look like:
    https://news.google.com/rss/articles/CBMiigFBVV95cUxNQ3ZDczhPdkl2...?oc=5

That opaque blob averages ~276 chars and carries no information the model can use,
yet it made up >50% of the weekly briefing payload. This module turns it into the
publisher URL (~100 chars), which is both shorter and actually readable.

How it works (no API key, no browser):
  1. GET the article page once -> it embeds a signature `data-n-a-sg` and a
     timestamp `data-n-a-ts` for that article.
  2. POST both to Google's internal `batchexecute` endpoint with the `Fbv4je` RPC
     ("garturlreq"), which returns the destination URL.
  Step 2 is batched: many articles resolve in a single HTTP round trip.

Results are cached in the unified news database and their `used_at` timestamp is refreshed on hits.
The curator removes mappings unused for 14 days, so the cost is normally paid once while an article
can still be relevant.

Failures are non-fatal: if resolution fails the original Google News URL is kept,
so a briefing never loses its link.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests

from .config import HTTP_TIMEOUT
from .log import get as _get_log
from .memory import Memory

log = _get_log("news.resolve")

BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_SIG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_RES = re.compile(
    r'garturlres\\",\\"(https?://[^\\"]+)\\",\d+\]",null,null,null,"c(\d+)"'
)
_ARTICLE_ID = re.compile(r"news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)")

# Tracking junk that bloats URLs without affecting where they point.
_JUNK_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "oc", "hl", "gl", "ceid", "greeting",
}


def _warn(msg: str) -> None:
    log.warn(msg)


def _cache_get(aids: list[str]) -> dict[str, str]:
    if not aids:
        return {}
    with Memory() as memory:
        return memory.urls_get(aids)


def _cache_put(pairs: dict[str, str]) -> None:
    if not pairs:
        return
    with Memory() as memory:
        memory.urls_put(pairs)


# ---------------------------------------------------------------- tidy
def tidy(url: str) -> str:
    """Strip tracking params and an empty query/fragment. Never changes the target."""
    try:
        p = urllib.parse.urlsplit(url)
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
                if k.lower() not in _JUNK_PARAMS]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(kept), ""))
    except Exception:
        return url


# ---------------------------------------------------------------- resolve
def _fetch_sig(aid: str) -> tuple[str, str, int] | None:
    """Fetch the article page to read its signature + timestamp."""
    try:
        r = requests.get(
            f"https://news.google.com/rss/articles/{aid}?oc=5",
            impersonate="chrome", timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        sg, ts = _SIG.search(r.text), _TS.search(r.text)
        if sg and ts:
            return aid, sg.group(1), int(ts.group(1))
    except Exception as e:
        _warn(f"sig {aid[:16]}: {e}")
    return None


def _batch(triples: list[tuple[str, str, int]]) -> dict[str, str]:
    """One batchexecute round trip for many articles."""
    envelopes = []
    for i, (aid, sg, ts) in enumerate(triples):
        inner = [
            "garturlreq",
            [["en-US", "US", ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"], None, None, 1, 1,
              "US:en", None, 180, None, None, None, None, None, 0, None, None,
              [1608992183, 723341000]],
             "en-US", "US", 1, [2, 3, 4, 8], 1, 0, "655000234", 0, 0, None, 0],
            aid, ts, sg,
        ]
        envelopes.append(["Fbv4je", json.dumps(inner), None, f"c{i}"])
    payload = "f.req=" + urllib.parse.quote(json.dumps([envelopes]))
    try:
        r = requests.post(
            BATCH_URL, data=payload, impersonate="chrome", timeout=HTTP_TIMEOUT * 2,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        r.raise_for_status()
    except Exception as e:
        _warn(f"batch of {len(triples)}: {e}")
        return {}
    found = _RES.findall(r.text)
    # batchexecute does NOT preserve request order — correlate via the "cN" tag
    # we assigned per envelope. Zipping positionally mismatches URLs to articles.
    out: dict[str, str] = {}
    for url, idx in found:
        i = int(idx)
        if 0 <= i < len(triples):
            out[triples[i][0]] = url
    return out


def resolve_many(urls: list[str], workers: int = 6, batch_size: int = 20) -> dict[str, str]:
    """Map Google News RSS URLs -> publisher URLs. Unresolvable ones are omitted."""
    aids: dict[str, str] = {}
    for u in urls:
        m = _ARTICLE_ID.search(u or "")
        if m:
            aids.setdefault(m.group(1), u)
    if not aids:
        return {}

    cached = _cache_get(list(aids))
    todo = [a for a in aids if a not in cached]

    fresh: dict[str, str] = {}
    if todo:
        triples = [t for t in ThreadPoolExecutor(workers).map(_fetch_sig, todo) if t]
        for i in range(0, len(triples), batch_size):
            fresh.update(_batch(triples[i:i + batch_size]))
        fresh = {a: tidy(u) for a, u in fresh.items()}
        _cache_put(fresh)
        log.info("resolved", new=len(fresh), attempted=len(todo), cache_hits=len(cached), total=len(aids))

    out: dict[str, str] = {}
    for aid, original in aids.items():
        real = cached.get(aid) or fresh.get(aid)
        if real:
            out[original] = real
    return out


def apply(items: list[dict]) -> list[dict]:
    """Rewrite item['url'] in place for Google News items. Keeps the original on failure."""
    gn = [i["url"] for i in items if "news.google.com/rss/articles/" in (i.get("url") or "")]
    if not gn:
        for i in items:
            if i.get("url"):
                i["url"] = tidy(i["url"])
        return items
    with log.span("resolve", urls=len(gn)):
        mapping = resolve_many(gn)
    for i in items:
        u = i.get("url") or ""
        i["url"] = tidy(mapping.get(u, u))
    return items
