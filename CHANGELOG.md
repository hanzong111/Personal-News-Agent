# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Optional **Jev mode** (TypeSafe's Jev model): catches repeat stories across outlets and languages,
  sends broker calls and stock lists to the digest instead of alerting, and types stories the keyword
  rules can't. Choose it in setup step 5 or with `setup set jev=on`; the built-in rules stay the
  default and the fallback. Benchmark in `docs/benchmarks/jev.md`, scripts in `benchmarks/jev/`.

### Added
- Jev mode marks follow-ups: an alert that updates a story you were alerted about in the past week
  gets a "🔄 Update on: <earlier headline>" line.

### Fixed
- Jev mode no longer drops updates as repeats. The repeat check now asks two questions per pair,
  "same story?" and "did something happen after the earlier report?" (agreement signed, deal
  completed, delay, value change, charges filed, …). Same story with nothing new = repeat (dropped);
  same story with something new = update (alerts, 🔄). Checked on 12 update/repeat cases (all
  correct) and re-scored on the 173 benchmark repeats: 149 still dropped, 22 now alert as updates
  (mostly real developments), 2 not the same story.

### Changed
- Renamed to **TickerPigeon**, with a new logo, the mascot Pip, a README banner and a dashboard
  favicon (assets in `docs/brand/`). The repository moves to `hanzong111/TickerPigeon`, and the
  installer's default folder is now `~/TickerPigeon`.

## [1.0.0] - 2026-09-28

First public release.

### Added
- Instant alerts for news and Bursa announcements naming a stock you hold or watch, every 30 min
  during market hours.
- Evening digest of sector and market news touching your stocks.
- Weekly review of each holding against the FBM KLCI, with a watchlist section.
- Malaysia headline index (politics, economy, policy, …), with "more <section>" follow-ups in chat.
- Setup flow, in the terminal (`python -m pipeline.setup`) or in chat (`/setup`): holdings,
  watchlist, chat app, and which messages arrive when. Stock lookup by name or code.
- Watchlist: stocks you don't hold get the same alerts, marked 👀.
- Chat app is a preference: Telegram, Discord, Slack, WhatsApp, Signal, Feishu and anything else
  Hermes Agent supports, with a test message.
- `bootstrap.sh` one-command installer (Linux, macOS, WSL2), plus `hermes/uninstall.sh`.
- Sector library of 16 common Bursa sectors, each matched to Yahoo Finance industry names.
- Local read-only dashboard, cost ledger, LSS6 tender watcher and chat model router (optional).
- 71 tests, run in CI on Python 3.10–3.12, plus shellcheck.
- Branch model: work lands on `develop` through `feature/…` and `fix/…` branches; `main` holds
  releases and only the maintainer merges into it. CI rejects pull requests into `main` that
  don't come from `develop`.
- Versioning: Semantic Versioning from the `VERSION` file, `scripts/bump_version.py` to cut a
  release, and a workflow that tags `vX.Y.Z` and publishes the GitHub Release when `main` changes.

[Unreleased]: https://github.com/hanzong111/TickerPigeon/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/hanzong111/TickerPigeon/releases/tag/v1.0.0
