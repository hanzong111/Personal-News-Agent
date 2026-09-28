"""Source adapters. Each returns a list of normalised item dicts:

  id, source, kind (news|announcement), title, summary, url, published (ISO, MYT),
  codes (holding codes this item is explicitly about), sectors (sector keys), impact (high|normal)

No LLM here. All HTTP via curl_cffi with Chrome impersonation (Bursa is Cloudflare-gated).
KLSE Screener requests are spaced by KLSE_CRAWL_DELAY to honour its robots.txt Crawl-delay.
"""
from __future__ import annotations
import html
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from curl_cffi import requests
from .config import HTTP_TIMEOUT, KLSE_CRAWL_DELAY, Config, Holding
from .log import get as _get_log

log = _get_log("fetch")

MYT = timezone(timedelta(hours=8))
KLSE = "https://www.klsescreener.com"
BURSA_ANN = "https://www.bursamalaysia.com/market_information/announcements/company_announcement/announcement_details?ann_id="


_last_klse = 0.0


def _throttle(url: str):
    """Honour KLSE Screener's `Crawl-delay: 20`. Measured finish-to-start, so retries are spaced too."""
    global _last_klse
    if KLSE_CRAWL_DELAY <= 0 or "klsescreener.com" not in url:
        return
    wait = _last_klse + KLSE_CRAWL_DELAY - time.monotonic()
    if wait > 0:
        log.debug("klse crawl-delay", wait=round(wait, 1))
        time.sleep(wait)


def _stamp(url: str):
    global _last_klse
    if "klsescreener.com" in url:
        _last_klse = time.monotonic()


def _get(url: str, tries: int = 2) -> requests.Response:
    last = None
    for attempt in range(tries):
        _throttle(url)
        try:
            r = requests.get(url, impersonate="chrome", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:          # transient DNS / 5xx: retry once
            last = e
            log.debug("http retry", url=url[:80], attempt=attempt + 1, err=str(e)[:80])
        finally:
            _stamp(url)
    raise last


def _warn(msg: str):
    log.warn(msg)


# ---------------------------------------------------------------- KLSE Screener per-stock news
_KLSE_ITEM = re.compile(
    r'<div class="item[^"]*">.*?<a target="_blank" href="(/v2/news/view/(\d+)[^"]*)">(.*?)</a></h2></div>\s*'
    r'<div>(.*?)</div>\s*<div class="item-title-secondary subtitle"><span>(.*?)</span>'
    r'<span data-date="([^"]+)"', re.S)


def klse_news(h: Holding) -> list[dict]:
    try:
        text = _get(f"{KLSE}/v2/news/stock/{h.code}").text
    except Exception as e:
        _warn(f"klse news {h.short}: {e}")
        return []
    out = []
    for href, nid, title, summary, source, date in _KLSE_ITEM.findall(text):
        try:
            pub = datetime.strptime(date.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=MYT)
        except ValueError:
            pub = datetime.now(MYT)
        out.append({
            "id": f"klse:{nid}",
            "source": f"{html.unescape(source).strip()} (via KLSE Screener)",
            "kind": "news",
            "title": html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
            "summary": html.unescape(re.sub(r"<[^>]+>", "", summary)).strip()[:400],
            "url": KLSE + href,
            "published": pub.isoformat(),
            "codes": [h.code], "sectors": [], "impact": "normal",
        })
    log.info("klse news", holding=h.short, items=len(out))
    return out


# ---------------------------------------------------------------- Bursa announcements API
_A_HREF = re.compile(r"ann_id=(\d+)")
_TAG = re.compile(r"<[^>]+>")


def bursa_announcements(h: Holding, cfg: Config) -> list[dict]:
    url = (f"https://www.bursamalaysia.com/api/v1/announcements/search"
           f"?ann_type=company&company={h.code}&per_page=20&page=1")
    try:
        rows = _get(url).json().get("data", [])
    except Exception as e:
        _warn(f"bursa {h.short}: {e}")
        return []
    out = []
    for row in rows:
        if len(row) < 4:
            continue
        _, date_html, _, title_html = row[:4]
        m = _A_HREF.search(title_html)
        if not m:
            continue
        ann_id = m.group(1)
        title = html.unescape(_TAG.sub("", title_html)).strip()
        if any(k.lower() in title.lower() for k in cfg.announcement_ignore):
            continue
        # date html: "<div class='d-lg-inline-block d-none'>18 Sep 2026</div>"
        dm = re.search(r"(\d{1,2} \w{3} \d{4})</div>\s*$", date_html)
        try:
            pub = datetime.strptime(dm.group(1), "%d %b %Y").replace(tzinfo=MYT) if dm else datetime.now(MYT)
        except ValueError:
            pub = datetime.now(MYT)
        high = any(k.lower() in title.lower() for k in cfg.announcement_high)
        out.append({
            "id": f"bursa:{ann_id}",
            "source": "Bursa Malaysia announcement",
            "kind": "announcement",
            "title": title, "summary": "",
            "url": BURSA_ANN + ann_id,
            "published": pub.isoformat(),
            "codes": [h.code], "sectors": [], "impact": "high" if high else "normal",
        })
    log.info("bursa announcements", holding=h.short, items=len(out), high=sum(1 for i in out if i["impact"] == "high"),
             ignored=len(rows) - len(out))
    return out


# ---------------------------------------------------------------- Google News RSS
def gnews(query: str, codes: list[str], sectors: list[str], days: int = 3) -> list[dict]:
    q = urllib.parse.quote_plus(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-MY&gl=MY&ceid=MY:en"
    try:
        root = ET.fromstring(_get(url).content)
    except Exception as e:
        _warn(f"gnews '{query}': {e}")
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3].strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(MYT)
        except Exception:
            pub = datetime.now(MYT)
        out.append({
            "id": f"gnews:{guid[-48:]}",
            "source": f"{src or 'Google News'} (via Google News)",
            "kind": "news",
            "title": html.unescape(title), "summary": "",
            "url": link,
            "published": pub.isoformat(),
            "codes": list(codes), "sectors": list(sectors), "impact": "normal",
        })
    log.info("google news", query=query[:50], items=len(out), codes=",".join(codes) or "-", sectors=",".join(sectors) or "-")
    return out


# ---------------------------------------------------------------- Prices (KLSE Screener quote page)
_PRICE = re.compile(r'id="price"[^>]*data-value="([\d.]+)"')
_DIFF = re.compile(r'id="priceDiff">\s*([-+]?[\d.]+)\s*\(([-+]?[\d.]+)%\)')
_DIR = re.compile(r'id="price_header"[^>]*class="[^"]*\b(increasing|decreasing)\b')


def prices(cfg: Config) -> dict[str, dict]:
    """code -> {price, change, pct}. One quote-page request per holding; missing on failure."""
    out = {}
    log.debug("prices …", holdings=len(cfg.holdings))
    for h in cfg.holdings:
        try:
            t = _get(f"{KLSE}/v2/stocks/view/{h.code}").text
            price = float(_PRICE.search(t).group(1))
            dm = _DIFF.search(t)
            chg, pct = (float(dm.group(1)), float(dm.group(2))) if dm else (0.0, 0.0)
            d = _DIR.search(t)
            if d and d.group(1) == "decreasing":
                chg, pct = -abs(chg), -abs(pct)
            out[h.code] = {"price": price, "change": chg, "pct": pct}
        except Exception as e:
            _warn(f"price {h.short}: {e}")
    log.info("prices", holdings=len(out), missing=len(cfg.holdings) - len(out))
    return out


# ---------------------------------------------------------------- Yahoo Finance daily history
def history(symbol: str, rng: str = "2mo") -> list[dict]:
    """Daily OHLCV rows (oldest first) for a Yahoo symbol, e.g. '0215.KL' or '^KLSE'. Holidays skipped."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval=1d"
    log.debug("yahoo history …", symbol=symbol)
    try:
        res = _get(url).json()["chart"]["result"][0]
    except Exception as e:
        _warn(f"history {symbol}: {e}")
        return []
    q = res["indicators"]["quote"][0]
    rows = []
    for ts, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]):
        if c is None:
            continue
        rows.append({"date": datetime.fromtimestamp(ts, MYT).date(), "open": o, "high": h, "low": l,
                     "close": c, "volume": v or 0})
    log.info("yahoo history", symbol=symbol, days=len(rows))
    return rows


# ---------------------------------------------------------------- Firehose (untagged general business news)
# These carry no stock/sector tag; the keyword matcher tags what it can and the rest goes to the
# daily Relevance Judge. This is what closes the "keyword blind spot" for industry news.
FIREHOSE_RSS = [
    ("The Malaysian Reserve", "https://themalaysianreserve.com/feed/"),
    ("BusinessToday", "https://www.businesstoday.com.my/feed/"),
    ("Malay Mail Money", "https://www.malaymail.com/feed/rss/money"),
]


def firehose() -> list[dict]:
    out = []
    for name, url in FIREHOSE_RSS:
        try:
            root = ET.fromstring(_get(url).content)
        except Exception as e:
            _warn(f"firehose {name}: {e}")
            continue
        for it in root.iter("item"):
            title = html.unescape((it.findtext("title") or "").strip())
            link = (it.findtext("link") or "").strip()
            desc = re.sub(r"<[^>]+>", "", html.unescape(it.findtext("description") or "")).strip()[:300]
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(MYT)
            except Exception:
                pub = datetime.now(MYT)
            if title and link:
                out.append({"id": f"rss:{(it.findtext('guid') or link).strip()[-48:]}", "source": name, "kind": "news",
                            "title": title, "summary": desc, "url": link, "published": pub.isoformat(),
                            "codes": [], "sectors": [], "impact": "normal"})
    try:
        j = _get("https://theedgemalaysia.com/api/loadMoreCategories?offset=0&categories=corporate").json()
        for r in j.get("results", []):
            pub = datetime.fromtimestamp(int(r.get("created", 0)) / 1000, MYT) if r.get("created") else datetime.now(MYT)
            out.append({"id": f"edge:{r.get('nid')}", "source": "The Edge Malaysia", "kind": "news",
                        "title": html.unescape(r.get("title") or "").strip(),
                        "summary": re.sub(r"<[^>]+>", "", html.unescape(r.get("summary") or "")).strip()[:300],
                        "url": f"https://theedgemalaysia.com/{r.get('alias', '')}", "published": pub.isoformat(),
                        "codes": [], "sectors": [], "impact": "normal"})
    except Exception as e:
        _warn(f"firehose edge: {e}")
    log.info("firehose", items=len(out), feeds=len(FIREHOSE_RSS) + 1)
    return out
