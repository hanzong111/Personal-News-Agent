#!/usr/bin/env bash
# Hermes cron wrapper (no-agent, zero tokens): LSS6 tender-result watcher. Silent unless something new matches.
cd "__PROJECT_DIR__" || exit 1
exec ./.venv/bin/python -m pipeline.lss6
