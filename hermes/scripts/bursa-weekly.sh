#!/usr/bin/env bash
# Hermes cron wrapper: weekly portfolio review (prices vs KLCI + week's news). Always wakes the agent.
cd "__PROJECT_DIR__" || exit 1
# Fires 5 min before the weekly time in data/preferences.yaml (default Fri 20:00). See bursa-digest.sh.
export BURSA_DELIVER_AT="prefs"
exec ./.venv/bin/python -m pipeline.weekly "$@"
