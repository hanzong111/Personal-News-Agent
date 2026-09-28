# Phase 1 — Research: Sources & Reachability

Probed 2026-09-20 from the local machine (Linux, residential IP) using plain `curl`,
headless Google Chrome, and Python `curl_cffi` with Chrome TLS impersonation.
Throwaway probes live in `scratch/` (venv at `scratch/.venv` has `curl_cffi`).

## Portfolio

The research was done against a real seven-stock Bursa portfolio: three solar EPCC small-caps,
two banks, one construction/infrastructure large-cap and one private-hospital operator. The mix
matters for the findings below: small caps rarely appear in Google News, so KLSE Screener and
Bursa filings carry them. The specific holdings aren't published here. Your own come from
`data/portfolio.yaml`, which setup writes.

**Industry themes to watch:** solar & RE policy (CRESS, LSS, NETR, SAC charges, battery
storage incentives), data-centre capex, OPR / Bank Negara, banking sector results,
Budget 2027 (tabled Oct 2026), construction contract flow (MRT3, Penang LRT, Sabah/Sarawak),
private healthcare regulation & medical tariffs.

## Reachability legend

- **curl** — plain HTTP client works (any agent web_fetch tool will work)
- **cffi** — needs Chrome TLS impersonation (`curl_cffi`, `impersonate="chrome"`); plain
  curl and headless Chrome get a Cloudflare "Just a moment" page
- **RSS/JSON** — structured feed available, no HTML parsing needed

## Channel matrix

| # | Channel | What it gives | Access | Verdict |
|---|---------|---------------|--------|---------|
| 1 | **KLSE Screener** `klsescreener.com/v2` | Per-stock news aggregation (`/news/stock/{code}`) pulling The Star, NST, The Edge, Nanyang, Sin Chew, Bernama…; per-stock announcements mirror (`/announcements/stock/{code}`); quote page (`/stocks/view/{code}`); price JSON (`/stocks/all.json`) | curl, HTML | **Primary backbone.** One site covers stock news + Bursa announcements + price for all 7 codes. No RSS; parse HTML (`div.item` blocks, 20 per page). |
| 2 | **Bursa Malaysia announcements API** `bursamalaysia.com/api/v1/announcements/search?ann_type=company&company={code}` | Official filings: contracts, results, shareholder changes, dealings | **cffi**, JSON (`data` rows: date, company, title, ann_id) | **Primary for filings.** Authoritative & timestamped; 403 without impersonation. `announcements.bursamalaysia.com` (PDF store) unreachable from here. |
| 3 | **Google News RSS** `news.google.com/rss/search?q={query}+when:7d&hl=en-MY&gl=MY&ceid=MY:en` | Cross-outlet search: Edge, Star, NST, Bernama, BusinessToday, Nikkei… | curl, **RSS** | **Primary for industry/keyword news.** Works per ticker and per theme ("solar Malaysia", "OPR", "data centre Johor"). Weak on small caps (Samaiden: 0 hits/7d) — KLSE Screener fills that gap. Links are Google redirect URLs. |
| 4 | **The Edge Malaysia** `theedgemalaysia.com/api/loadMoreCategories?offset=0&categories={corporate\|malaysia}` | Latest corporate news, JSON with title/summary/created/alias | curl, **JSON** | **Secondary.** Category feed works; no search/tag endpoint found. Article pages (`/node/{nid}`) load full text, no paywall seen on test article (some Edge Weekly pieces are premium). |
| 5 | **i3investor** `klse.i3investor.com/web/stock/news/{code}` | Per-stock news + forum/blog chatter | **cffi**, HTML | Secondary / redundant with #1. Useful for retail sentiment later. |
| 6 | **Yahoo Finance** `query1.finance.yahoo.com/v8/finance/chart/{code}.KL` | OHLCV history, useful for daily % change & charts | **cffi**, JSON (429 with plain curl) | Backup price source. KLSE Screener price suffices for v1. |
| 7 | **The Star Business** `thestar.com.my/business` | Index page | curl, HTML | No RSS found (`/rss/*` all 404). Covered via #1 and #3 — don't scrape directly. |
| 8 | **NST Business** | — | curl, HTML | No RSS found. Covered via #1 and #3. |
| 9 | **Bernama Business** `bernama.com/en/business/` | Wire news | curl, HTML | No RSS at guessed paths. Covered via #3 (BernamaBiz appears in Google News). |
| 10 | **Malay Mail Money** `malaymail.com/feed/rss/money` | General biz | curl, **RSS** (site itself needs cffi) | Low priority; general macro only. |
| 11 | **The Malaysian Reserve** `themalaysianreserve.com/feed/` | Biz daily | curl, **RSS** | Nice-to-have industry coverage. |
| 12 | **BusinessToday** `businesstoday.com.my/feed/` | Biz portal, stock picks | curl, **RSS** | Nice-to-have; appears in Google News anyway. |
| 13 | **Free Malaysia Today** `cms.freemalaysiatoday.com/feed` | General | curl, **RSS** | Low priority. |
| 14 | investing.com, Bursa Marketplace | — | 403 | Skip. |

## Shortlist for the pipeline

1. **KLSE Screener per-stock news + announcements** — stock-specific alerts (all 7).
2. **Bursa announcements API** — authoritative filings (contract wins, results, dealings).
3. **Google News RSS** — one query per stock + one per industry theme.
4. **The Edge corporate JSON** + **TMR / BusinessToday RSS** — industry colour.
5. **KLSE Screener quote page** (fallback Yahoo chart) — prices for the daily brief.

Everything is free, keyless, and low volume (< 50 HTTP requests per poll cycle).

## Hermes Agent capabilities confirmed

| Need | Hermes feature | Notes |
|------|----------------|-------|
| Scheduling | `hermes cron create "<schedule>" "<prompt>"` — supports `every 30m`, `weekdays at 9am`, cron exprs | Gateway daemon ticks every 60s; jobs in `~/.hermes/cron/jobs.json` |
| Zero-LLM polling | `hermes cron create "every 15m" --no-agent --script poll.py --deliver telegram` | Script stdout = message; empty stdout = silent tick; last line `{"wakeAgent": false}` skips LLM. Scripts must live in `$HERMES_HOME/scripts/`. **Ideal for fetch+dedup; wake the model only when new items exist.** |
| Delivery | `--deliver telegram:<chat_id>` / `discord:#chan` / `all` | Telegram set up via `hermes gateway setup` → `hermes gateway start` |
| Web access | Built-in web search/fetch/browser tools (Firecrawl via Nous Portal, or bring-your-own keys) | Our sources are plain HTTP/JSON, so a python script with `curl_cffi` is more reliable than agent browsing |
| Models | Nous Portal, OpenRouter, OpenAI, custom endpoints; `/model provider:model` | Enables cheap model for classify/filter, stronger for brief writing |
| Skills / memory | Agent-authored skills; persistent cross-session memory | Use for "why it matters to the holdings" context and learning what the user cares about |
| Hosting | Local, Docker, SSH, Modal, Daytona, Vercel Sandbox | Needs always-on host for cron — decide in Phase 2 |

Docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron ·
https://hermes-agent.nousresearch.com/docs/user-guide/messaging/ ·
https://github.com/NousResearch/hermes-agent

## Open items carried into Phase 2

- Cloudflare-gated sources (#2, #5, #6) need `curl_cffi` — must run as a script, not via the
  agent's generic fetch tool. Confirm this still works from the eventual host (VPS IPs are
  more likely to be challenged than a residential IP).
- Google News links are redirect URLs; decide whether to resolve them or just show source + title.
- Chinese-language items (Nanyang, Sin Chew) come through KLSE Screener — the model can
  translate/summarise; decide whether to include them.
- Announcement noise: substantial-shareholder changes (S138) fire almost daily for CIMB/RHB —
  need a category filter/whitelist.
- Verify The Edge paywall behaviour on more articles; fall back to `summary` field if blocked.
