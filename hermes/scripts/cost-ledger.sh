#!/usr/bin/env bash
# Hermes cron wrapper (no-agent): snapshot Hermes token counters into data/state/costs.db. Always silent.
cd "__PROJECT_DIR__" || exit 1
exec ./.venv/bin/python -m pipeline.costs collect
