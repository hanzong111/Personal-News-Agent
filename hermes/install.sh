#!/usr/bin/env bash
# Install the Hermes-side pieces of this project into a Hermes Agent home (default ~/.hermes):
#   scripts/*.sh          -> $HERMES_HOME/scripts/      (cron wrappers, project path filled in)
#   skills/*/SKILL.md     -> $HERMES_HOME/skills/<name>/ (chat skills, project path filled in)
#   plugins/model-router  -> $HERMES_HOME/plugins/      (only with --with-router)
# It also creates the project venv if it is missing. Your stocks + message times come from
# `python -m pipeline.setup` (or /setup in chat) afterwards.
# Existing files are left alone unless --force is given (then the old file is kept as *.bak).
# Cron jobs are NOT created here — see hermes/cron/create-jobs.sh.
#
#   ./hermes/install.sh [--force] [--with-router]
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
FORCE=0; ROUTER=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --with-router) ROUTER=1 ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

[ -d "$HERMES_HOME" ] || { echo "Hermes home not found: $HERMES_HOME (install Hermes Agent first)" >&2; exit 1; }

# Render a template: replace __PROJECT_DIR__ and write to dest (respecting --force).
render() {
  local src="$1" dest="$2"
  if [ -e "$dest" ] && [ "$FORCE" -eq 0 ]; then echo "  skip   $dest (exists; --force to overwrite)"; return; fi
  [ -e "$dest" ] && cp -p "$dest" "$dest.bak"
  mkdir -p "$(dirname "$dest")"
  sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$src" > "$dest"
  [ -x "$src" ] && chmod +x "$dest"
  echo "  wrote  $dest"
}

echo "Project: $PROJECT_DIR"
echo "Hermes:  $HERMES_HOME"

echo "[1/4] Python venv"
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/python" -m pip install -q -r "$PROJECT_DIR/requirements.txt"

echo "[2/4] Portfolio"
if [ -f "$PROJECT_DIR/data/portfolio.yaml" ]; then
  echo "  data/portfolio.yaml exists — kept"
else
  echo "  none yet — the setup step (below) asks for your stocks and writes it"
fi

echo "[3/4] Cron wrappers + skills"
for f in "$PROJECT_DIR"/hermes/scripts/*.sh; do render "$f" "$HERMES_HOME/scripts/$(basename "$f")"; done
for d in "$PROJECT_DIR"/hermes/skills/*/; do
  n="$(basename "$d")"; render "$d/SKILL.md" "$HERMES_HOME/skills/$n/SKILL.md"
done

echo "[4/4] model-router plugin"
if [ "$ROUTER" -eq 1 ]; then
  for f in plugin.yaml __init__.py router.py; do
    render "$PROJECT_DIR/hermes/plugins/model-router/$f" "$HERMES_HOME/plugins/model-router/$f"
  done
else
  echo "  skipped (pass --with-router to install)"
fi

echo
echo "Next:"
echo "  1. ./hermes/cron/create-jobs.sh                          # create the scheduled jobs (once)"
echo "  2. ./.venv/bin/python -m pipeline.setup                  # stocks, watchlist, chat app, message times"
echo "Or skip step 2 and message the bot /setup — it runs the same setup in chat."
