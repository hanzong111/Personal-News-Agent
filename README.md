# Personal News Agent — Bursa Malaysia

A self-hosted news agent for **Bursa Malaysia** investors, built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It watches the news for the stocks
you hold, a watchlist of stocks you're considering, and the sectors they belong to. Short,
skimmable briefs are pushed to your chat app (Telegram, Discord, Feishu, and others). You don't
need to ask.

A short setup, either in the terminal or by chatting with the bot, asks for your stocks, your chat
app, and which messages you want at what times. That turns it into your own newsletter.

It sends a message only when news names one of your holdings or touches its sector. It never
sends "quiet day" filler.

> Research aid only, not financial advice.

## What you get

| Message | When | Content |
|---|---|---|
| **Holding alert** | Every 30 min, 08:00–18:30, Mon–Fri. Silent if nothing is new. | News or Bursa announcements naming a stock you hold (or watch, marked 👀), with sentiment, a 1–2 line summary, why it matters, the link and today's price. |
| **Evening digest** | 18:30 Mon–Fri. Skipped if empty. | Sector and macro news for the industries you hold (policy, commodity, contract flow, Budget…), with the holdings each item touches. |
| **Weekly review** | Friday 20:00 | Each holding's week against the FBM KLCI, what moved and why, what to watch next week, suggestions and cautions. |
| **Malaysia headlines** | 09:00 / 14:00 / 21:00 daily | A tiered index of general Malaysian news (politics, economy, policy, incidents…). Reply "more politics" for the full list. |
| **Chat Q&A** | Any time | "What do I hold?", "I bought IJM", "watch Inari", "move the digest to 7pm", "anything new on Gamuda?" |

The times shown are the defaults. You choose which of these you get, and when, during setup.

Optional extras: an LSS6 (Large Scale Solar) tender-result watcher, a token-cost ledger, and a
local web dashboard.

## How it works

```
 sources (no LLM)                 filter (code first)               write             deliver
┌───────────────────────┐   ┌───────────────────────────┐   ┌────────────────┐   ┌──────────────┐
│ KLSE Screener news    │   │ alias / keyword match     │   │ briefer        │   │ Hermes cron  │
│ Bursa announcements   │──▶│ dedup (title + URL + DB)  │──▶│ digest writer  │──▶│ → Telegram / │
│ Google News RSS       │   │ relevance judge (Haiku)   │   │ editor         │   │   Discord /  │
│ The Edge, Star, RSS…  │   │  for what the rules miss  │   │ renderer(code) │   │   Feishu …   │
│ Yahoo prices          │   └───────────────────────────┘   └────────────────┘   └──────────────┘
└───────────────────────┘                │
                                         ▼
                            data/state/news.db  (SQLite memory: seen items,
                            pending queue, delivered stories, notes, URL cache)
```

- **Fetching and matching are plain Python.** No tokens are spent to decide whether an item
  mentions a holding. A cheap model (Haiku) looks only at items the keyword rules couldn't place.
- **Models write words, code writes facts.** Agents return JSON verdicts keyed by item id. The
  deterministic renderer (`agents/renderer.py`) fills in links, times and prices from stored
  data, so a model can never attach the wrong article to a headline.
- **Nothing is lost on a crash.** An item leaves the queue only after its message has been
  written, and the next run picks up anything left pending.
- **Hermes does the scheduling and delivery.** Each job is a Hermes cron job running a small
  wrapper script. Most jobs are `--no-agent`: the script's stdout *is* the message, and empty
  stdout means nothing is sent.

## Quick install

On Linux, macOS or Windows (in WSL2), run:

```bash
curl -fsSL https://raw.githubusercontent.com/hanzong111/Personal-News-Agent/main/bootstrap.sh | bash
```

This installs everything that's missing and walks you through setup (details below). It asks
before it uses `sudo` or changes a system setting, and it's safe to run again. Nothing is repeated
or duplicated.

If you'd rather read the script before running it:

```bash
git clone https://github.com/hanzong111/Personal-News-Agent.git
cd Personal-News-Agent
less bootstrap.sh
./bootstrap.sh
```

Options: `--yes` (answer yes to install questions), `--dir DIR` (where to clone, default
`~/Personal-News-Agent`), `--deliver discord` (pre-select your chat app), `--with-router` (install
the chat model router), `--skip-hermes-setup`.

## Prerequisites

`bootstrap.sh` installs every item marked *auto* for you. The others are accounts or settings only
you can provide.

| What | Why | How you get it |
|---|---|---|
| **Linux, macOS, or Windows + WSL2** | The pipeline is Python plus bash wrappers run by Hermes cron. | Windows: in PowerShell as admin, `wsl --install`, then run the installer inside Ubuntu. Native Windows isn't supported. |
| **git, curl** | Fetch the code and the installers. | *auto* (apt / dnf / yum / pacman / zypper / apk / Homebrew) |
| **Python ≥ 3.10 with `venv`** | Runs the pipeline in its own `.venv`. | *auto*: the system package, or `uv`'s Python 3.11 when the system one is too old (e.g. macOS's 3.9) |
| **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | Schedules the jobs, calls the AI models and delivers messages to your chat app. | *auto*: the [official installer](https://hermes-agent.nousresearch.com), which brings its own uv, Python 3.11 and Node |
| **An Anthropic account** | The agents use Claude models (Haiku / Sonnet / Opus). | An [Anthropic API key](https://console.anthropic.com/) or a Claude subscription login. You enter it during `hermes setup` (step 4 of the installer). |
| **A chat app bot** | Where your alerts arrive. | Any app Hermes supports: Telegram, Discord, Slack, WhatsApp, Signal, Feishu/Lark, WeChat, Matrix and more. Connect it in `hermes setup` (or later with `hermes setup gateway`), then pick it during our setup. Telegram is quickest: message [@BotFather](https://t.me/BotFather), `/newbot`, paste the token. |
| **Malaysia time zone** | Schedules are read as local time. | *auto*: the installer offers to switch to `Asia/Kuala_Lumpur`, or you keep your zone and enter local times during setup. |
| **A machine that's on** | Hermes cron runs locally, and missed runs catch up after sleep. | A laptop that's on during market hours works. For 24/7 use a small always-on box (mini PC, Raspberry Pi 5, a cheap VPS). |

It needs about 3 GB of disk: Hermes Agent with its own Python and Node toolchain is ~2.7 GB, and this project's venv is ~60 MB and outbound HTTPS to the news sources,
Yahoo Finance, Anthropic and your chat app.

## What the installer does

`bootstrap.sh` runs eight steps. Each step checks first and skips work that's already done.

| Step | What happens |
|---|---|
| 1. System packages | Installs git, curl and Python (+ `venv`) if missing |
| 2. Get the code | Clones this repo, or updates your clone with `git pull --ff-only` |
| 3. Hermes Agent | Runs the official installer if `hermes` isn't on your PATH |
| 4. AI provider + chat app | `hermes setup`: pick Anthropic, log in or paste a key, connect your bot |
| 5. Project install | Creates `.venv`, installs `requirements.txt`, copies the cron wrappers and chat skills into `~/.hermes` (`hermes/install.sh`) |
| 6. Time zone | Checks for UTC+8 and offers to switch |
| 7. Scheduled jobs | Creates the Hermes cron jobs (`hermes/cron/create-jobs.sh`) and makes sure the gateway (the scheduler) runs as a service. Where there's no systemd (some WSL setups, containers), it tells you to use `hermes gateway run` instead. |
| 8. Your stocks and messages | The setup wizard: holdings, watchlist, chat app, which messages and when. It ends with a test message. |

### Manual install (same steps, by hand)

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash   # Hermes Agent
hermes setup                                                         # AI provider + chat app
git clone https://github.com/hanzong111/Personal-News-Agent.git && cd Personal-News-Agent
./hermes/install.sh                                                  # venv, deps, wrappers, skills
./hermes/cron/create-jobs.sh                                         # scheduled jobs
hermes gateway install && hermes gateway start                       # scheduler as a service
./.venv/bin/python -m pipeline.setup                                 # stocks, chat app, message times
```

You can skip the last line and send **/setup** to your bot instead, which runs the same setup in
chat. If Hermes lives somewhere other than `~/.hermes`, export `HERMES_HOME` first.

Until you've picked at least one stock, every job stays silent. The first scan after setup is
silent too: it records what's already published, so you aren't flooded with old news. Alerts
start from the next new story.

### What the setup asks

```
Step 1/4 · Stocks you hold
  Stock name or code (Enter when done): gamuda
    ✓ 5398 GAMUDA — Gamuda Bhd · sector: Construction / infrastructure / data centres
  Stock name or code (Enter when done): sunway reit
    1. 5211 SUNWAY — Sunway Bhd · sector: Construction …
    2. 5176 SUNREIT — Sunway Real Estate Investment Trust · sector: REITs
    Which one? (number, Enter to skip): 2

Step 2/4 · Watchlist — stocks you don't hold but want news on (alerts marked 👀)
  Stock name or code (Enter when done): inari
    ✓ 0166 INARI — Inari Amertron Bhd · sector: Technology / semiconductors / EMS

Step 3/4 · Chat app — where your messages arrive
  Connected in Hermes: 1. Telegram  2. Discord
  Send my messages to [telegram]: 2

Step 4/4 · Which messages, and when (Malaysia time)
  Instant alerts — news naming a stock you hold or watch, checked every 30 min
    Receive these? [Y/n]
    Alert hours, first-last (scans at :00 and :30) [8-18]: 9-17
  Evening digest — sector and market news touching your stocks, once a day
    Time [18:30]: 7pm
  Weekly review — your stocks' week vs the KLCI, what moved and why, what to watch
    Receive these? [Y/n]: n
  Malaysia headlines — general Malaysian news index
    Times, comma-separated [09:00,14:00,21:00]: 09:00,21:00
```

Stocks are looked up by name or code on Yahoo Finance. Setup fills in the Bursa code, short name,
headline aliases, a news search query and a suggested sector. You confirm the sector or pick
another. The results go to `data/portfolio.yaml` and `data/preferences.yaml` (both gitignored), and
setup moves the Hermes cron jobs to your chosen times.

## Configuration

### `data/portfolio.yaml`: your stocks

This file is the single source of truth for what you hold and watch. Setup writes it, and you
can edit it by hand too. It is read fresh on every run, so no restart is needed. It is
**gitignored**; `data/portfolio.example.yaml` shows the format.

`holdings` are stocks you own. `watchlist` entries use the same fields for stocks you're
watching. They get the same instant alerts (marked 👀), and the weekly review covers them in a
short section of their own, framed as possible buys.

```yaml
holdings:
  - code: "3336"                 # Bursa stock code (KLSE Screener / Bursa lookups)
    short: IJM                   # Bursa short name
    name: IJM Corporation Bhd
    sector: construction         # must be a key in sectors.yaml
    aliases: [IJM Corp, "IJM "]  # headline matches, case-insensitive; trailing space = whole word
    queries: ['"IJM Corp"']      # Google News searches for stock-specific news
    # qty / avg_cost             optional, for your own reference
```

Look up the code and short name at `https://www.klsescreener.com/v2/stocks/view/<code>`.
Choose aliases carefully. Short names that appear inside other words (`TNB`, `IJM`) need the
trailing space, or you'll get false alerts.

From the command line or chat:

```bash
./.venv/bin/python -m pipeline.setup add "kpj"             # or a code: add 5878
./.venv/bin/python -m pipeline.setup add "top glove" --watch
./.venv/bin/python -m pipeline.setup remove IJM
./.venv/bin/python -m pipeline.setup status
```

### `data/preferences.yaml`: which messages, and when

| Message | Settings | Default |
|---|---|---|
| `alerts` | `enabled`, `hours` (first-last hour, scans at :00 and :30), `days` (`weekdays` / `daily`) | on, 8-18, weekdays |
| `digest` | `enabled`, `time`, `days` | on, 18:30, weekdays |
| `weekly` | `enabled`, `day` (`mon`..`sun`), `time` | on, fri, 20:00 |
| `headlines` | `enabled`, `times` (list, all on the same minute) | on, 09:00 / 14:00 / 21:00 |
| `deliver` | the chat app every message goes to: `telegram`, `discord`, `slack`, `whatsapp`, `signal`, `feishu`, … or `app:chat_id` for a specific group or channel | the first app connected in Hermes |

Change them with `setup set`, which saves the file and updates the Hermes jobs in one step:

```bash
./.venv/bin/python -m pipeline.setup set digest.time=19:00 weekly=off headlines.times=08:00,20:00
./.venv/bin/python -m pipeline.setup set deliver=discord     # move every message to Discord
./.venv/bin/python -m pipeline.setup apps                    # chat apps connected in Hermes
./.venv/bin/python -m pipeline.setup test                    # send a test message there
```

After editing the file by hand, run `python -m pipeline.setup apply`. Each job fires a few
minutes early and holds its message until the chosen time, so it lands on the minute.

### `data/sectors.yaml`: industry themes

Each sector a holding points to has:

- `label`: display name
- `queries`: Google News searches for sector-level news (fed into the evening digest)
- `keywords`: case-insensitive substrings that tag a general-feed headline as belonging to the sector

A `macro` block (Budget, KLCI, OPR…) applies to every holding. `announcement_ignore` and
`announcement_high` tune which Bursa filings are noise and which are always important.
Sectors with no holdings are ignored.

The shipped file is a library of common Bursa sectors: solar, construction, healthcare, gloves,
banking, plantation, technology, REITs, property, oil & gas, telco, utilities, consumer,
transport, gaming and materials. Only sectors your stocks point to are scanned. Each sector's
`match` list (Yahoo industry names) lets setup suggest a sector for a new stock. Stocks that fit
none go under `other` and get their own news only. Add a block to cover a new theme.

### Environment variables

All are optional. See `.env.example` for the full list with defaults. The ones you're most
likely to change:

| Variable | Default | What |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Where Hermes keeps state, cron jobs and its code |
| `AGENT_BACKEND` | `hermes` | `hermes` = one-shot `hermes chat` calls. `anthropic` = direct SDK (needs `ANTHROPIC_API_KEY` and `pip install anthropic`); slightly cheaper, no Hermes overhead |
| `BURSA_LOOKBACK_HOURS` | `72` | How far back a scan looks for new items |
| `BURSA_DELIVER_AT` | `prefs` (set in the wrappers) | Hold output until the time in `preferences.yaml`. Leave it unset for manual runs, which never wait |
| `BURSA_KLSE_CRAWL_DELAY` | `20` | Seconds between KLSE Screener requests (its robots.txt asks for 20) |
| `CURATOR_LLM` | `1` | `0` = nightly curation runs without the model |

Set per-job variables in the wrapper scripts under `hermes/scripts/`, then re-run
`./hermes/install.sh --force`.

### Models

The agents in `agents/` pin their own models (`agents/llm.py`):

| Role | Model | Job |
|---|---|---|
| judge, classifier, curator, memory keeper | Haiku | Labelling and relevance. Cheap, high volume |
| briefer, editor, digest writer | Sonnet | Deciding what matters and writing summaries |
| weekly review | Opus (set on the cron job) | Weekly analysis |
| renderer | none (code) | Final message layout, links, prices |

### Schedules and chat app

Message times and the chat app come from `data/preferences.yaml` (see above), and `setup apply`
pushes them to every job this project created. The housekeeping jobs (`bursa-curate`, `bursa-ops`
and the extras) keep fixed schedules from `hermes/cron/create-jobs.sh`, but they also follow your
chat app. Only apps connected in Hermes can receive: `setup status` warns when the chosen one isn't
connected.

### Optional: chat model router

`hermes/plugins/model-router` is a Hermes plugin. For each incoming chat message, a Haiku judge
labels it easy or hard. Hard questions (stock analysis, buy/sell, comparisons) run on Opus;
easy ones run on your default model. Install it with `./hermes/install.sh --with-router`.

## Usage

### From chat

Just talk to your Hermes bot. The `bursa-setup`, `bursa-portfolio` and `malaysia-news` skills handle:

- "/setup": the onboarding conversation (stocks, watchlist, messages and times)
- "I bought KPJ" / "I sold IJM" / "watch Top Glove"
- "move the digest to 7pm" / "stop the weekly review" / "headlines only at 9am"
- "send my alerts to Discord instead"
- "what do I hold?" / "how are my stocks doing?"
- "anything new on Gamuda?" / "catch me up"
- "why didn't I get an alert today?"
- "more economy" / "tell me about the haze story"

Chat answers from what the cron jobs stored. It doesn't browse the web itself.

### From the command line

Run these from the project directory with `./.venv/bin/python -m …`:

```bash
pipeline.setup                         # the setup wizard; `pipeline.setup --help` for the commands
pipeline.setup status                  # stocks, message times, whether Hermes jobs match
pipeline.scan --status                 # holdings + live prices
pipeline.scan --dry-run                # what would alert right now (commits nothing)
pipeline.scan --dry-run --force-recent 10   # last 10 holding items, seen or not
pipeline.digest --dry-run              # tonight's digest as it would be sent
pipeline.digest --dry-run --raw        # the queued items behind it (no model call)
pipeline.weekly                        # raw weekly review data
pipeline.news_index --preview          # Malaysia headline index preview
pipeline.news_index --more economy     # full list for one section

pipeline.memory stats                  # what's in the news memory
pipeline.memory query gamuda --days 30 # search stored items
pipeline.curate --dry-run --no-llm     # what clustering / pruning would do

pipeline.log runs -n 10                # recent runs: duration, LLM calls, tokens, result
pipeline.log show <run-id>             # one run as a timeline
pipeline.log tail --level WARN         # recent warnings/errors
pipeline.costs report --by-day         # token spend per job and model (needs cost-ledger)

pipeline.dashboard --port 9120         # local web console → http://127.0.0.1:9120
```

`hermes cron run <job-id>` fires a real job immediately.

## Project layout

```
bootstrap.sh        one-shot installer: prerequisites, Hermes, project, jobs, setup
pipeline/           fetch → match → memory → render; one module per cron job
  setup.py            setup wizard + commands   prefs.py       message times → cron schedules
  portfolio.py        stock lookup, portfolio.yaml editing
  scan.py             holding alerts            digest.py      evening digest
  weekly.py           weekly review data        news_index.py  Malaysia headline index
  curate.py           story clustering + prune  ops.py         watchdog
  fetchers.py         all HTTP sources          match.py       alias/keyword tagging, dedup
  memory.py           SQLite news memory        hold.py        deliver-at-time hold
  gnews_resolve.py    Google News → real URLs   lss6.py        LSS6 tender watcher
  costs.py, log.py    cost ledger, structured logs
  dashboard/          read-only web console
agents/             single-purpose LLM roles (JSON in, JSON out) + llm.py backend
hermes/             everything that gets installed into ~/.hermes
  install.sh          venv, portfolio, wrappers, skills, plugin
  scripts/            cron wrapper scripts (templated project path)
  skills/             chat skills: bursa-setup, bursa-portfolio, malaysia-news
  plugins/            model-router
  cron/               create-jobs.sh + the weekly-review prompt
data/
  portfolio.example.yaml   format reference; setup writes portfolio.yaml (gitignored)
  preferences.yaml         written by setup (gitignored)
  sectors.yaml             sector library: themes, news queries, industry matching
  state/, logs/            runtime, gitignored
docs/               research notes and the design plan
tests/              pytest suite
```

## Development

```bash
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q
```

The tests use fakes and temp databases. They don't hit the network, call a model or touch
your Hermes install.

## Notes and limits

- **Scraping etiquette.** Sources are public listing pages and RSS feeds. Article bodies are
  never scraped for alerts. KLSE Screener's crawl delay is honoured. Keep the scan interval
  reasonable.
- **Sources change.** Site layouts and APIs (especially Bursa's) change without notice. `bursa-ops`
  posts a warning when a source starts failing.
- **Malaysia-specific.** Market, currency (MYR), time zone and news sources are hard-wired for
  Bursa Malaysia.
- **Cost.** Most runs are no-agent and spend nothing when there's no news. Typical spend is a
  few model calls per alert plus one per digest and weekly review. Check
  `pipeline.costs report` for your actual numbers.

## License

MIT. See [LICENSE](LICENSE).
