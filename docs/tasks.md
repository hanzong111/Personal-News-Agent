# Build tasks — status 2026-09-20

- [x] Project venv (`.venv`: curl_cffi, pyyaml)
- [x] `data/portfolio.yaml` (7 holdings) + `data/sectors.yaml` (4 sectors + macro + filters)
- [x] Fetchers: KLSE Screener news, Bursa announcements API, Google News RSS, KLSE quote prices
- [x] Matcher: title-alias tiering, sector keywords, cross-source title dedup, 72 h freshness
- [x] `scan.py` / `digest.py` with `wakeAgent:false` silent path; seen.db bootstrapped (193 items)
- [x] Hermes wrappers in `~/.hermes/scripts/`
- [x] `bursa-portfolio` skill
- [x] Cron jobs `bursa-scan`, `bursa-digest` → Telegram
- [x] Emoji/sentiment template, two-line headers, 🕒 timestamps, markdown links; `cron.wrap_response: false`
- [x] `bursa-weekly` (Fri 20:00): Yahoo weekly OHLCV vs KLCI + news → analysis, suggestions, cautions
- [x] End-to-end test: forced items → LLM → Telegram delivered (execution ledger: `delivered`)

- [x] Agent refactor: `agents/` (llm backend, classifier, editor, renderer, judge); Malaysia index as
      no-agent cron with deterministic layout; firehose feeds + daily Relevance Judge in the digest
- [x] `malaysia-news` skill v2 (`--more`, `--pool`, `--article`)

## Next / optional
- [x] Stock Briefer → JSON agent + renderer; bursa-scan no-agent; attribution rule; cross-run alert dedup
- [x] Ops watchdog (`bursa-ops`, no-agent, every 30 min)
- [ ] Split Digest writer / Analyst into JSON agents + renderer
- [ ] `AGENT_BACKEND=anthropic` once an API key exists (drops the Hermes hop entirely)
- [ ] Watch the first real week; tune `aliases`, sector `keywords`, `announcement_ignore`
- [ ] Optional: price-move alert (holding moves > X% with no news) — not requested yet
- [ ] Optional: The Edge corporate JSON + TMR/BusinessToday RSS as extra general feeds
- [ ] Optional: resolve Google News redirect links to real URLs
- [ ] If moving to a VPS: verify Bursa API reachability (Cloudflare) from that IP
