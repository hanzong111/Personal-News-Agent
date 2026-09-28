---
name: bursa-portfolio
description: "Bursa Malaysia holdings: what the user owns, sectors, live prices, add/remove stocks, check for news."
version: 1.0.0
author: hanzong111
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Bursa, KLSE, stocks, portfolio, Malaysia, news, alerts]
---

# Bursa Portfolio

The user holds Bursa Malaysia (KLSE) stocks and gets automatic news alerts via the
`bursa-scan` / `bursa-digest` cron jobs. This skill is how you answer questions about the
portfolio and keep it up to date. **The portfolio file is the source of truth — never rely
on memory for what the user holds; read the file.**

Project dir: `__PROJECT_DIR__`
Python: `./.venv/bin/python` (run commands from the project dir)

## Files

| File | What |
|------|------|
| `data/portfolio.yaml` | `holdings` + `watchlist` (👀, not held): `code`, `short`, `name`, `sector`, `aliases`, `queries` |
| `data/preferences.yaml` | Which messages the user gets and when (managed by `pipeline.setup`) |
| `data/sectors.yaml` | Sector themes → Google News `queries` + `keywords`; announcement noise/high lists |
| `data/state/news.db` | Unified sqlite memory: dedup keys, pending work, delivered items, stories, notes, resolved URLs. Don't edit directly. |

## When to use

- "What do I hold?", "what's my portfolio", "what sector is X in", "how are my stocks doing"
- "I bought / sold X", "add X to my portfolio", "remove X" → via the bursa-setup skill
- "Anything new on my stocks?", "check the news now"
- Adjusting what counts as news (aliases, sector keywords, announcement filters)

## Commands

```bash
cd "__PROJECT_DIR__"

# holdings + live prices (no side effects)
./.venv/bin/python -m pipeline.scan --status

# what's new right now, without marking anything as seen or queuing
./.venv/bin/python -m pipeline.scan --dry-run

# show the last N holding-related items regardless of seen state (good for "catch me up")
./.venv/bin/python -m pipeline.scan --dry-run --force-recent 10

# the evening digest exactly as it would be sent (one Sonnet call, nothing committed) / from the last 48h
./.venv/bin/python -m pipeline.digest --dry-run
BURSA_LOOKBACK_HOURS=48 ./.venv/bin/python -m pipeline.digest --preview
# the raw queued items behind it (no model call)
./.venv/bin/python -m pipeline.digest --dry-run --raw

# weekly review data (prices vs KLCI + week's news) — the raw input of the Friday job
./.venv/bin/python -m pipeline.weekly

# inspect managed news memory (read-only commands)
./.venv/bin/python -m pipeline.memory stats
./.venv/bin/python -m pipeline.memory query gamuda --days 30

# show what story clustering / retention would do without changing anything
./.venv/bin/python -m pipeline.curate --dry-run --no-llm
```

`{"wakeAgent": false}` on the last line just means "nothing new" — tell the user that.

## Chat budget (every tool call re-reads the whole conversation)

- Aim for **≤3 tool calls per reply**. Pick the one command that answers; don't explore.
- Cost / token questions: run `./.venv/bin/python -m pipeline.costs report` once and answer from it.
  Never read source files, grep the code or query state.db to re-derive or "double-check" its numbers.
- Pipeline status: `./.venv/bin/python -m pipeline.log runs -n 10` (+ `show <run>` if needed).
- If a question really needs code investigation or debugging, say so in one line and stop — that work
  belongs in Claude Code on the laptop, not in chat.

## Answering news questions in chat (no web search)

Chat has no web/browser tools on purpose: the cron jobs do all fetching. Answer from what they stored:
- Bursa holdings / sectors: `./.venv/bin/python -m pipeline.memory query <term> --days N` (title, stage, url).
- General Malaysian news (malaysia-news job): `grep -i <term> data/state/news/pool.jsonl` (JSON lines: time, src, title, url).
If nothing matches, say the pipeline hasn't picked it up — don't guess. Keep replies short; chat context is small.

## "What happened in the last run?" / "why didn't I get an alert?"

```bash
./.venv/bin/python -m pipeline.log runs -n 10          # recent runs: duration, LLM calls, tokens, result
./.venv/bin/python -m pipeline.log show scan-20260921-080000   # one run as a timeline (prefix match ok)
./.venv/bin/python -m pipeline.log tail --level WARN -n 30     # recent warnings/errors across all nodes
```
Read the `gate` line (alerts/queued/judge counts) and any `alert item` lines to explain what was or wasn't sent.

## Adding / removing stocks, watchlist, message times

Use the **bursa-setup** skill — it owns every change to `portfolio.yaml` and `preferences.yaml`:

```bash
./.venv/bin/python -m pipeline.setup add "<name or code>" [--watch] [--sector KEY]
./.venv/bin/python -m pipeline.setup remove <code|short>
./.venv/bin/python -m pipeline.setup set digest.time=19:00 weekly=off
./.venv/bin/python -m pipeline.setup status
```

Watchlist stocks (`--watch`) are not held: they get the same alerts, marked 👀. If `status` prints
`Set up: no`, run the bursa-setup onboarding conversation before answering portfolio questions.

## Google News URL resolution (`pipeline/gnews_resolve.py`)

Google News RSS links are opaque redirect blobs averaging ~276 chars. They were >50% of
the weekly briefing payload and carry no information. `gnews_resolve.apply()` runs at the
end of `scan.collect()` and rewrites them to the real publisher URL (~100 chars), then
strips tracking params. Measured: weekly run 19,900 -> 14,296 chars (~1,400 tokens saved).

How it resolves (no API key, no browser):
1. GET the article page -> read `data-n-a-sg` (signature) and `data-n-a-ts` (timestamp).
2. POST both to `news.google.com/_/DotsSplashUi/data/batchexecute` with the `Fbv4je`
   RPC (`garturlreq`). Batched ~20 per round trip.

**Pitfall — batchexecute does NOT return results in request order.** Each envelope is
tagged `c0, c1, c2...` and responses come back scrambled (observed `c2,c3,c1,c0,c6,c5,c4`).
Correlate on the `cN` tag; zipping positionally silently attaches the WRONG article to
each URL, which looks fine until you click a link and get an unrelated story.

Resolved URLs are cached in the `urls` table of `data/state/news.db`; successful cache hits
refresh `used_at`, and entries unused for 14 days are pruned by `bursa-curate`. **If a
correlation bug is ever fixed, clear that table** — poisoned mappings otherwise look valid.
Failures are non-fatal and are not cached: the original Google News URL is kept so a briefing
never loses its link. Typical resolve rate ~85%.

Verify a change by comparing each item's title against the live page `<title>` — use
`./.venv/bin/python` (curl_cffi impersonation), since plain curl gets Cloudflare-walled
on businesstoday/smartkarma and returns a useless "Just a moment...".

## Token budget of a briefing

Measure before optimising. On a typical weekly run the payload splits roughly:
URLs ~53%, titles ~13%, structure/prices ~10%, **summaries only ~5%**.
The 400-char summary cap (`fetchers.py`) is the only part carrying real meaning and is a
rounding error in tokens — do not trim it. The pipeline never scrapes article bodies;
it reads listing pages and RSS only, so title+summary is already the minimum payload.
Remaining wins are cross-source dedup (one event can appear from 8 outlets) and dropping
`summary` for tier-2 sector items.

## Cron jobs (already created; manage with `hermes cron list|pause|resume|run`)

Times and on/off below are the defaults — the user's choices live in `data/preferences.yaml`
(`pipeline.setup status` shows them). Change them with the bursa-setup skill, not `hermes cron edit`.

- `bursa-scan` — every 30 min, weekdays 08:00–18:30 MYT. Alerts on news naming a holding.
- `bursa-digest` — 18:30 MYT weekdays. Sector/macro items collected that day. Silent if empty.
- `bursa-curate` — 19:00 MYT weekdays. Clusters stories, refreshes notes, prunes fixed retention windows. Silent on success.
- `bursa-weekly` — Friday 20:00 MYT. Week's price action vs KLCI + news → analysis, suggestions, cautions.
