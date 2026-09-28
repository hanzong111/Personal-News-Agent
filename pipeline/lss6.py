"""LSS6 watcher: be first to hear when the Large Scale Solar 6 tender results (shortlisted bidders) are out.

No model in the loop — plain HTTP + keyword rules, so polling every 30 min 24/7 costs zero tokens. Silent
unless something new matches. Sources:
  1. Bursa announcements, ALL companies, keyword search (winners file "shortlisted bidder … Large Scale Solar").
     Any new hit alerts.
  2. Google News LSS6 / large-scale-solar queries. Alerts only on result wording (shortlisted, successful
     bidders, awarded, wins…); speculative headlines ("tipped", "expected") come as a heads-up.
  3. Energy Commission (st.gov.my) newsroom + programme list: they mention LSS nowhere today, so any new
     LSS text or link alerts.
First run records what already exists (no alert). State: data/state/lss6.json.

    python -m pipeline.lss6 [--dry-run] [--rebaseline]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import urllib.parse
from datetime import datetime

from . import config, fetchers
from .fetchers import MYT, _get
from .log import get as _get_log, new_run

log = _get_log("lss6")
STATE = config.STATE / "lss6.json"
SILENT = json.dumps({"wakeAgent": False})

BURSA_KEYWORDS = ["LSS6", "LSS 6", "Large Scale Solar", "shortlisted bidder"]
GNEWS_QUERIES = ["LSS6", '"LSS 6" solar', '"large scale solar" Malaysia shortlisted', "LSS6 successful bidders",
                 '"LSS6" pembida berjaya']
ST_PAGES = ["https://www.st.gov.my/newsroom", "https://www.st.gov.my/ms/ruang-berita",
            "https://www.st.gov.my/sustainability/energy-transition-programmes"]

CONTEXT = re.compile(r"LSS\s?6|\bLSS\b|large[- ]scale solar|solar berskala besar|solar tender", re.I)
STRONG = re.compile(r"shortlist|successful (?:\w+ )?bidder|selected (?:\w+ )?bidder|letter of award|\bLOA\b|\bawarded\b|award list|"
                    r"\bwinners?\b|results? (?:are )?(?:out|announced|released|unveiled)|pembida berjaya|"
                    r"disenarai pendek|keputusan", re.I)
WEAK = re.compile(r"\b(?:wins?|won|bags?|secures?|secured|clinch(?:es|ed)?|lands?|landed|nabs?)\b", re.I)
SPECULATIVE = re.compile(r"tipped|expected|expects|could|\bmay\b|likely|poised|eyes|targets?|bid(?:ding)? for|hopes|"
                         r"aims?|seen as|potential|beneficiar|to know|await|soon|by (?:end|september|october)", re.I)
ST_LSS = re.compile(r"LSS\s?6|\bLSS\b|Large Scale Solar|Solar Berskala Besar", re.I)


def classify(title: str) -> str | None:
    """'result' | 'heads-up' | None for a news headline."""
    if not CONTEXT.search(title):
        return None
    strong, weak, spec = STRONG.search(title), WEAK.search(title), SPECULATIVE.search(title)
    if strong or weak:
        return "heads-up" if spec else "result"
    return None


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _bursa(keyword: str) -> list[dict]:
    url = ("https://www.bursamalaysia.com/api/v1/announcements/search?ann_type=company"
           f"&keyword={urllib.parse.quote(keyword)}&per_page=20&page=1")
    out = []
    for row in _get(url).json().get("data", []):
        if len(row) < 4:
            continue
        m = re.search(r"ann_id=(\d+)", str(row[3]))
        text = lambda h: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(h)))).strip()
        if not m:
            continue
        date = re.findall(r"\d{1,2} \w{3} \d{4}", str(row[1]))
        out.append({"id": f"bursa:{m.group(1)}", "company": text(row[2]), "title": text(row[3]),
                    "when": date[-1] if date else "", "url": fetchers.BURSA_ANN + m.group(1)})
    return out


def _st_fingerprint(url: str) -> dict:
    """LSS sentences + LSS-looking links on an ST page (scripts/styles stripped)."""
    page = _get(url).text
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
    snippets = sorted({text[max(0, m.start() - 80):m.end() + 120].strip() for m in ST_LSS.finditer(text)})
    links = sorted({h for h in re.findall(r'href="([^"]+)"', page) if re.search(r"lss|large-scale-solar|berskala", h, re.I)})
    return {"snippets": snippets, "links": links}


def _holdings_named(cfg, text: str) -> list[str]:
    """Holdings named in text; an alias with a trailing space is whole-word only (same rule as match.py)."""
    def hit(alias: str) -> bool:
        a = alias.strip()
        if not a:
            return False
        return bool(re.search(rf"\b{re.escape(a)}\b", text, re.I)) if alias.endswith(" ") or len(a) <= 5 \
            else a.lower() in text.lower()
    return [h.short for h in cfg.holdings if any(hit(a) for a in [h.short, h.name, *h.aliases])]


def check(cfg, state: dict) -> tuple[list[str], dict]:
    """Return (alert lines, new state). Every source is independent: one failing never hides the others."""
    seen = set(state.get("seen", []))
    st_prev = state.get("st", {})
    first = not state
    alerts: list[str] = []
    new_seen = set(seen)
    ok = {"bursa": 0, "gnews": 0, "st": 0}

    for kw in BURSA_KEYWORDS:
        try:
            rows = _bursa(kw)
            ok["bursa"] += 1
        except Exception as e:
            log.warn("bursa search failed", keyword=kw, err=str(e)[:120])
            continue
        for r in rows:
            if r["id"] in new_seen:
                continue
            new_seen.add(r["id"])
            if not first:
                names = _holdings_named(cfg, f"{r['company']} {r['title']}")
                alerts.append(f"🚨 **Bursa filing** — {r['company']}\n{r['title'][:220]}\n🕒 {r['when']}"
                              + (f"\n🎯 Your holding: {', '.join(names)}" if names else "") + f"\n🔗 [Announcement]({r['url']})")

    for q in GNEWS_QUERIES:
        try:
            items = fetchers.gnews(q, [], ["solar"], days=7)
            ok["gnews"] += 1
        except Exception as e:
            log.warn("gnews failed", query=q, err=str(e)[:120])
            continue
        for i in items:
            key = "g:" + hashlib.sha1(i["title"].lower().encode()).hexdigest()[:16]   # same story, many URLs
            if key in new_seen:
                continue
            new_seen.add(key)
            kind = classify(i["title"])
            if first or not kind:
                continue
            names = _holdings_named(cfg, i["title"])
            icon = "🚨 **LSS6 result?**" if kind == "result" else "👀 **LSS6 heads-up**"
            alerts.append(f"{icon} — {i['source'].split(' (via')[0]}\n{i['title'][:220]}\n🕒 {i['published'][:16].replace('T', ' ')}"
                          + (f"\n🎯 Your holding: {', '.join(names)}" if names else "") + f"\n🔗 [Article]({i['url']})")

    st_now = {}
    for url in ST_PAGES:
        try:
            st_now[url] = _st_fingerprint(url)
            ok["st"] += 1
        except Exception as e:
            log.warn("st page failed", url=url, err=str(e)[:120])
            st_now[url] = st_prev.get(url, {"snippets": [], "links": []})
            continue
        prev = st_prev.get(url, {"snippets": [], "links": []})
        new_bits = [s for s in st_now[url]["snippets"] if s not in prev["snippets"]] + \
                   [l for l in st_now[url]["links"] if l not in prev["links"]]
        if new_bits and not first and url in st_prev:
            alerts.append(f"🚨 **Energy Commission site mentions LSS** — {url.split('st.gov.my')[1]}\n"
                          + "\n".join(f"• {b[:200]}" for b in new_bits[:4]) + f"\n🔗 [Open page]({url})")

    log.info("check", first=first, alerts=len(alerts), bursa_ok=ok["bursa"], gnews_ok=ok["gnews"], st_ok=ok["st"],
             seen=len(new_seen))
    if not any(ok.values()):
        raise RuntimeError("every LSS6 source failed")        # fails the cron job -> bursa-ops alerts
    return alerts, {"seen": sorted(new_seen)[-3000:], "st": st_now, "since": state.get("since") or
                    datetime.now(MYT).isoformat(timespec="minutes")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="check and print, don't save state")
    ap.add_argument("--rebaseline", action="store_true", help="forget state; next run records without alerting")
    a = ap.parse_args(argv)
    new_run("lss6")
    if a.rebaseline:
        STATE.unlink(missing_ok=True)
        log.info("silent", reason="rebaselined")
        return
    cfg = config.load()
    state = _load()
    alerts, new_state = check(cfg, state)
    if not a.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(new_state, ensure_ascii=False))
    if not alerts:
        log.info("silent")
        print(SILENT)
        return
    text = f"☀️ **LSS6 watch** — {datetime.now(MYT):%a %d %b %H:%M}\n\n" + "\n\n".join(alerts)
    log.info("message", alerts=len(alerts), chars=len(text))
    print(text)


if __name__ == "__main__":
    main()
