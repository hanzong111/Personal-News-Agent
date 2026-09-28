#!/usr/bin/env bash
# Hermes cron wrapper: cluster delivered stories, refresh compact notes, then prune by fixed policy.
cd "__PROJECT_DIR__" || exit 1
exec ./.venv/bin/python -m pipeline.curate "$@"
