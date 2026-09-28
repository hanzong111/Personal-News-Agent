#!/usr/bin/env bash
# Hermes cron wrapper (no-agent): auto-/new for idle chat sessions (memory pass first). Always silent.
cd "__PROJECT_DIR__" || exit 1
exec ./.venv/bin/python -m pipeline.chat_sweep --idle-hours 1
