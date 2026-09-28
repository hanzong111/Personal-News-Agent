# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Branch model: work happens on `feature/…` branches merged into `develop`; `main` holds releases
  and only the maintainer merges into it. CI runs on both and rejects PRs into `main` from
  anything but `develop`. Dependabot targets `develop`.

## [0.1.0] - 2026-09-28

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
- 66 tests, run in CI on Python 3.10–3.12, plus shellcheck.

[Unreleased]: https://github.com/hanzong111/Personal-News-Agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hanzong111/Personal-News-Agent/releases/tag/v0.1.0
