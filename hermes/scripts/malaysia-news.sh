#!/usr/bin/env bash
# Hermes cron wrapper (no-agent): fetch → CLASSIFIER → EDITOR → RENDERER → stdout = the message.
# Empty stdout = nothing new, nothing sent. Agents are invoked inside via agents/llm.py.
cd "__PROJECT_DIR__" || exit 1
# Fires 10 min before each headline time in data/preferences.yaml (default 09:00/14:00/21:00).
export BURSA_DELIVER_AT="prefs"
exec ./.venv/bin/python -m pipeline.news_index --cron
