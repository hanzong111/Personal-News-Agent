#!/usr/bin/env bash
# Hermes cron wrapper (no-agent): pipeline watchdog. Empty stdout = all good.
cd "__PROJECT_DIR__" || exit 1
exec ./.venv/bin/python -m pipeline.ops
