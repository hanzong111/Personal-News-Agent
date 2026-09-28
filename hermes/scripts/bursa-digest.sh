#!/usr/bin/env bash
# Hermes cron wrapper (no-agent): digest_writer + code renderer print the finished evening digest
# (links filled from stored items, never written by a model). Silent if nothing to send.
cd "__PROJECT_DIR__" || exit 1
# The job fires 5 min before the digest time in data/preferences.yaml (default 18:30);
# hold.until_target("digest") pins the stdout write — and the delivery that follows it — to that time.
# A late/catch-up run emits immediately instead.
export BURSA_DELIVER_AT="prefs"
exec ./.venv/bin/python -m pipeline.digest "$@"
