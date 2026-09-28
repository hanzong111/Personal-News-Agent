# Phase 2/3 — Pipeline Plan (FINAL, built 2026-09-20)

Built directly from `docs/research.md`. Phases 2–4 were collapsed on the user's instruction.

## Principle

Briefs go out **only when news names a holding or its sector**. No calendar briefs, no "quiet
day" messages. Price moves ride along as context, never trigger on their own.

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │         YOU  (your chat app)        │
                    │  "I bought 1000 TENAGA"             │
                    │  "anything new on my stocks?"       │
                    └───────────────┬─────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                HERMES AGENT   gateway = systemd user service              │
│                                                                           │
│  INTERACTIVE                     CRON  bursa-scan    */30 8-18 Mon-Fri    │
│  skill: bursa-portfolio          CRON  bursa-digest  18:30   Mon-Fri      │
│  read/edit portfolio.yaml        CRON  bursa-curate  19:00   Mon-Fri      │
│  run status / memory query       CRON  bursa-weekly  Fri 20:00            │
└──────────────┬──────────────────────────────────────┬─────────────────────┘
               │                                      │
               ▼                                      ▼
┌─────────────────────────────┐   ┌───────────────────────────────────────┐
│  MEMORY  data/              │   │  PIPELINE  pipeline/  (no LLM)        │
│  portfolio.yaml  holdings   │◀──│  fetchers  KLSE Screener news         │
│  sectors.yaml    themes     │   │            Bursa announcements API    │
│  state/news.db staged memory│   │            Google News RSS            │
│   keys · items · stories    │   │            KLSE quote page (prices)   │
│   notes · URL cache         │   │  curate   cluster · notes · prune     │
└─────────────────────────────┘   │  match     alias-in-title → tier 1    │
                                  │            sector kw / mention → 2    │
                                  │  render    grouped text for the LLM   │
                                  └───────────────────────────────────────┘
```

## Data flow per scan

```
fetch (≈45 HTTP req)  →  fresh? (<72h)  →  dedup id + title  →  tag
   tier 1  (holding named in title, or Bursa filing)  →  stage alert  →  briefer → chat now
   tier 2  (sector / macro / body-mention)            →  stage digest
   tier 0                                             →  stage judge
   nothing tier 1  →  {"wakeAgent": false}  →  silent, zero tokens

18:30 digest: read pending → judge in chunks → render → stdout → advance only after success
19:00 curate: cluster delivered stories → refresh rolling notes → deterministic 7/14/180 d prune
```

## Components

| Piece | Where | Role |
|-------|-------|------|
| `data/portfolio.yaml` | project | holdings, sector key, aliases, GNews queries |
| `data/sectors.yaml` | project | sector queries + keywords; announcement ignore/high lists |
| `pipeline/fetchers.py` | project | curl_cffi adapters; normalised item dicts |
| `pipeline/match.py` | project | tiering rules, title dedup |
| `pipeline/render.py` | project | text block injected into the agent prompt |
| `pipeline/scan.py` | project | alert entry point (`--status`, `--dry-run`, `--force-recent N`, `--bootstrap`) |
| `pipeline/digest.py` | project | digest entry point (`--dry-run`, `--preview`) |
| `pipeline/weekly.py` | project | weekly review: Yahoo OHLCV per holding + `^KLSE`, week's news; always wakes the agent |
| `pipeline/memory.py`, `pipeline/curate.py` | project | staged sqlite API/CLI; story clustering, notes, retention |
| `~/.hermes/scripts/bursa-{scan,digest,weekly,curate}.sh` | Hermes | wrappers: `cd` project, exec `.venv` python |
| `~/.hermes/skills/bursa-portfolio/SKILL.md` | Hermes | agent's how-to for reading/editing the portfolio |
| cron `bursa-scan`, `bursa-digest`, `bursa-curate` (19:00), `bursa-weekly` (Fri 20:00) | Hermes | schedules; `--deliver <your chat app>` |

## Model routing

> **Amended 2026-09-20 (evening):** provider is now `anthropic` (Claude Code OAuth). Scan/digest cron
> jobs run `claude-sonnet-5`; weekly runs the default `claude-opus-5`. The rows below were measured on
> `gpt-5.6-sol` and are kept for the token-size estimates only. Live state: `docs/dev/context.md`.

| Stage | Runs on | Tokens |
|-------|---------|--------|
| fetch / dedup / match / render | Python, no LLM | 0 |
| quiet tick | nothing (`wakeAgent: false`) | 0 |
| alert or digest with items | `gpt-5.6-sol` via openai-codex, reasoning `low` | ≈18k in / ≈0.5k out per message (measured) |
| weekly review (Friday) | `gpt-5.6-sol`, reasoning `medium` | ≈27k in / ≈1.4k out (measured) |
| curator (weekdays, at most one call) | cheap model, reasoning `low` | ≈12k in / ≈2k out planned maximum |
| chat (any Hermes chat app) | default model + `bursa-portfolio` skill | as normal |

## Hosting

This machine. `hermes-gateway.service` (systemd --user, linger enabled) ticks cron every 60 s;
`cron.catch_up_missed: true` re-fires missed runs after sleep/reboot. Machine must be awake
during 08:00–20:00 MYT on weekdays. Moving to a VPS = copy project dir + venv + the four wrapper
scripts + skill, re-run `hermes cron create`; re-check Bursa/Cloudflare from the VPS IP.

## Known trade-offs

- Sell-side notes ("RHB upbeat on X") pass the title rule; the LLM prompt is told to skip them.
- Bursa announcements carry a date only (no time) — they sort behind same-day news.
- Google News links are resolved to publisher URLs and cached for 14 days since last use.
- Chinese-language items (Nanyang, Sin Chew, Chinapress) are kept; the LLM translates.
- Small caps (e.g. solar EPCC names) rarely hit Google News; KLSE Screener + Bursa filings cover them.

## Agent architecture (refactor 2026-09-20)

One agent per judgement, code for everything else. Agents live in `agents/`, never call each
other, and hand off through files. Invoked through `agents/llm.py` — `hermes chat -Q -t none`
(tool-less one-shot, ~800 tokens of overhead) today; set `AGENT_BACKEND=anthropic` +
`ANTHROPIC_API_KEY` to go direct with the same prompts.

```
 Malaysia index (cron malaysia-news, --no-agent, 09:00 / 14:00 / 21:00)
   pipeline/news_fetch.collect  ──▶ agents/classifier (Haiku)  ──▶ dedup_en (code)
     feeds → dedup → window          section · sentiment · risk ·
     → cluster → resolve URLs        malaysian? · English title
                                  ──▶ agents/editor (Sonnet)  ──▶ agents/renderer (code) ──▶ stdout ──▶ your chat app
                                      threads · highlights ·        exact layout, hard caps
                                      collapse (JSON)
   state: data/state/news/{seen,last_run,pool,index}  ·  follow-ups: news_index --more, news_fetch --pool/--article

 Bursa (cron bursa-scan / bursa-digest / bursa-curate / bursa-weekly)
   fetchers (+ firehose: TMR, BusinessToday, Malay Mail Money, The Edge corporate)
     → match: tier 1 alert · tier 2 digest · tier 0 judge → data/state/news.db
   scan: agents/briefer → deterministic alert renderer; digest: judge in 60-item chunks
   curate: rule clusters → agents/curator semantic merges/notes → deterministic prune
   weekly: prices + delivered memory, collapsed by story id
```

| Agent | Model | In → Out | Cost/run (measured) |
|---|---|---|---|
| Classifier | Haiku 4.5 | story titles → labels JSON (chunks of 60) | ~$0.05 for 150 stories |
| Editor | Sonnet 5 | classified stories → threads/highlights/collapse JSON | ~$0.045 |
| Renderer | — | labels + plan → message text | 0 |
| Relevance Judge | Haiku 4.5 | untagged headlines + portfolio → rescued items | ~$0.02/day |

Split 2026-09-21: **Stock Briefer** (`agents/briefer.py`, Sonnet, JSON per item: keep/type/sentiment/risk/headline/
summary/why) + `renderer.render_alert` — `bursa-scan` is now a no-agent cron (~3k tokens/call instead of ~22k).
Matcher gained an attribution-only rule (alias as source ≠ subject → tier 2) and alerts have a 48 h cross-run story
memory (`news.db`, exact-title/recent-story rules plus rolling note context).
**Ops watchdog** (`pipeline/ops.py`, no LLM, cron `bursa-ops` every 30 min → chat): failed/undelivered/overrun
(> 2× interval) executions, ERROR events, degraded feeds, missing prices, gateway down. Each finding reported once.
Not yet split: Digest writer, Analyst (weekly).

## Logging (2026-09-21)

Every node logs ROS2-style through `pipeline/log.py`:

```
[INFO ] [2026-09-21 00:34:31.471] [llm] call done role=classifier model=claude-haiku-4-5 tok_in=2068 tok_out=1529 usd=0.01 dur=17.50
```

- Sinks: **stderr** (live; never stdout — stdout is the delivered message), `data/logs/pipeline.log`
  (same lines + run id), `data/logs/events.jsonl` (structured, for visualisation). 5 MB rotation.
- Nodes: `scan` `digest` `weekly` `fetch` `news.fetch` `news.resolve` `news.index` `agent.classifier`
  `agent.editor` `agent.judge` `llm` `renderer`. LLM calls record the Hermes session id and pull the
  real token counts + cost estimate from `~/.hermes/state.db`.
- One cron invocation = one `run` id (`scan-YYYYMMDD-HHMMSS`, `news-…`, `digest-…`, `weekly-…`).
- Viewer: `python -m pipeline.log runs` · `show <run>` (timeline, relative timestamps, duration bars) ·
  `tail [-n N] [--node agent.editor] [--run X] [--level WARN]`.
- `PIPELINE_LOG_LEVEL=DEBUG` on the command line shows feed-by-feed and HTTP-retry lines on stderr
  (they are always in the files).
