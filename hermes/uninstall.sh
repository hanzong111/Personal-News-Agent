#!/usr/bin/env bash
# Remove what this project added to Hermes: its cron jobs, the wrapper scripts, the chat skills and
# the model-router plugin. Hermes Agent itself, its config and your chat apps are left alone.
# This checkout (with your data/ folder) is kept too — delete the folder yourself if you want it gone.
#
#   ./hermes/uninstall.sh [--yes]
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
JOBS=(bursa-scan bursa-digest bursa-weekly malaysia-news bursa-curate bursa-ops lss6-watch cost-ledger chat-sweep cost-report-2wk)
SCRIPTS=(bursa-scan bursa-digest bursa-weekly malaysia-news bursa-curate bursa-ops lss6-watch cost-ledger chat-sweep cost-report)
SKILLS=(bursa-setup bursa-portfolio malaysia-news)

if [ "${1:-}" != "--yes" ]; then
  echo "This removes from $HERMES_HOME:"
  echo "  cron jobs:  ${JOBS[*]} (those that exist)"
  echo "  scripts:    ${SCRIPTS[*]/%/.sh}"
  echo "  skills:     ${SKILLS[*]}"
  echo "  plugin:     model-router"
  read -r -p "Continue? [y/N] " a
  [[ "$a" =~ ^[Yy] ]] || { echo "Nothing removed."; exit 0; }
fi

jobs_file="$HERMES_HOME/cron/jobs.json"
if [ -f "$jobs_file" ] && command -v hermes >/dev/null; then
  python3 - "$jobs_file" "${JOBS[@]}" <<'PY' | while read -r id name; do hermes cron remove "$id" >/dev/null && echo "  removed job $name"; done
import json, sys
d = json.load(open(sys.argv[1])); jobs = d["jobs"] if isinstance(d, dict) else d
for j in jobs:
    if j.get("name") in sys.argv[2:]:
        print(j["id"], j["name"])
PY
fi
for s in "${SCRIPTS[@]}"; do rm -f "$HERMES_HOME/scripts/$s.sh" "$HERMES_HOME/scripts/$s.sh.bak"; done
for k in "${SKILLS[@]}"; do rm -rf "${HERMES_HOME:?}/skills/$k"; done
rm -rf "${HERMES_HOME:?}/plugins/model-router"
echo "Done. Hermes Agent is still installed; remove it with its own uninstaller if you like."
echo "Your stocks and news history are still in $(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)/data."
