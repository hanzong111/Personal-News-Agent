#!/usr/bin/env bash
# Hermes cron wrapper (no-agent): holding + watchlist news alerts. Empty stdout / {"wakeAgent": false} = silent.
cd "__PROJECT_DIR__" || exit 1
exec ./.venv/bin/python -m pipeline.scan "$@"
