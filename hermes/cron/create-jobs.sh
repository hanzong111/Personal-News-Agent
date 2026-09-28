#!/usr/bin/env bash
# Create the Hermes cron jobs this project runs on. Run once, after hermes/install.sh.
# Schedules are in the machine's local time zone and assume Malaysia time (Asia/Kuala_Lumpur).
# Jobs are created with the default times; at the end, `pipeline.setup apply` moves them to the
# times (and on/off choices) in data/preferences.yaml, if you already ran the setup.
#
#   ./hermes/cron/create-jobs.sh             # core jobs
#   ./hermes/cron/create-jobs.sh --extras    # + LSS6 watcher, cost ledger, chat sweep
#
# Messages go to the chat app chosen in setup (`deliver` in data/preferences.yaml); before setup,
# to the first chat app connected in Hermes. DELIVER=discord (any Hermes target) overrides both.
# Safe to re-run: jobs that already exist (by name) are skipped.
# Manage afterwards with: hermes cron list | run <id> | pause <id> | remove <id>
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PROJECT_DIR="$(cd "$HERE/../.." && pwd)"
if [ -z "${DELIVER:-}" ]; then
  DELIVER="$(cd "$PROJECT_DIR" && ./.venv/bin/python -m pipeline.setup target 2>/dev/null || true)"
fi
if [ -z "$DELIVER" ]; then
  echo "No chat app to deliver to: connect one in Hermes (hermes setup gateway), or pass DELIVER=telegram." >&2
  exit 1
fi
echo "Delivering to: $DELIVER"
EXTRAS=0; [ "${1:-}" = "--extras" ] && EXTRAS=1

JOBS_FILE="${HERMES_HOME:-$HOME/.hermes}/cron/jobs.json"
exists() {  # a job with this name is already registered (makes re-running this script safe)
  [ -f "$JOBS_FILE" ] && python3 - "$JOBS_FILE" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); jobs = d["jobs"] if isinstance(d, dict) else d
sys.exit(0 if any(j.get("name") == sys.argv[2] for j in jobs) else 1)
PY
}

job() {  # job <name> <schedule> <script> [extra hermes flags...]   (agent prompt: set PROMPT)
  local name="$1" sched="$2" script="$3"; shift 3
  if exists "$name"; then echo "= $name  (already exists, skipped)"; return; fi
  echo "+ $name  ($sched)"
  # positional order is `schedule [prompt]` — the prompt must come after the schedule
  hermes cron create --name "$name" --script "$script" --deliver "$DELIVER" "$@" "$sched" ${PROMPT:+"$PROMPT"}
}

# Core: news alerts, digest, weekly review, general Malaysia headlines, housekeeping.
job bursa-scan    '*/30 8-18 * * 1-5' bursa-scan.sh    --no-agent   # alert when news names a holding
job bursa-digest  '25 18 * * 1-5'     bursa-digest.sh  --no-agent   # 18:30 sector/macro digest
job bursa-curate  '0 19 * * 1-5'      bursa-curate.sh  --no-agent   # cluster stories, prune memory
job bursa-ops     '*/30 * * * *'      bursa-ops.sh     --no-agent   # watchdog; silent when healthy
job malaysia-news '50 8,13,20 * * *'  malaysia-news.sh --no-agent   # 09:00/14:00/21:00 headline index
# Weekly review is the one job where a Hermes agent writes the message from the script's data.
PROMPT="$(cat "$HERE/prompts/bursa-weekly.md")" \
job bursa-weekly  '55 19 * * 5'       bursa-weekly.sh  --model claude-opus-5 --reasoning-effort medium

if [ "$EXTRAS" -eq 1 ]; then
  job lss6-watch  '5,35 * * * *'      lss6-watch.sh    --no-agent   # Large Scale Solar 6 tender results
  job cost-ledger '*/10 * * * *'      cost-ledger.sh   --no-agent   # snapshot Hermes token usage
  job chat-sweep  '15,45 * * * *'     chat-sweep.sh    --no-agent   # auto-/new idle chat sessions
fi

# Follow the user's chosen times / on-off / chat app, if setup has been run already.
if [ -f "$PROJECT_DIR/data/preferences.yaml" ]; then
  echo "+ applying data/preferences.yaml"
  (cd "$PROJECT_DIR" && ./.venv/bin/python -m pipeline.setup apply)
fi
