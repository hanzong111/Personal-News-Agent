#!/usr/bin/env bash
# TickerPigeon — one-shot installer. Installs what's missing, then walks you through setup.
#
#   curl -fsSL https://raw.githubusercontent.com/hanzong111/TickerPigeon/main/bootstrap.sh | bash
#   # or, from a clone:
#   ./bootstrap.sh [--yes] [--dir DIR] [--deliver APP] [--with-router] [--skip-hermes-setup]
#
# Steps (each is skipped when already done, so re-running is safe):
#   1. system packages   git, curl, python3 (+ venv)           via apt / dnf / yum / pacman / zypper / apk / brew
#   2. the code          clone (or update) into --dir           default ~/TickerPigeon
#   3. Hermes Agent      official installer, if `hermes` is missing
#   4. Hermes config     `hermes setup`: AI provider + chat app (Telegram, Discord, Feishu, …)
#   5. project install   Python venv + deps, cron wrappers + chat skills into ~/.hermes
#   6. time zone         schedules assume Malaysia time (Asia/Kuala_Lumpur)
#   7. scheduled jobs    hermes/cron/create-jobs.sh, then make sure the gateway (the scheduler) runs
#   8. your newsletter   `python -m pipeline.setup`: stocks, watchlist, chat app, message times (moves the
#                        jobs to your choices; until you've picked stocks, every job stays silent)
#
# Supported: Linux, macOS, Windows via WSL2. Anything that needs sudo or changes system settings
# asks first; --yes answers yes to those questions (your stock picks are still asked). Without a
# terminal to ask and without --yes, those steps are skipped.
set -euo pipefail

REPO_URL="https://github.com/hanzong111/TickerPigeon.git"
DIR="${HOME}/TickerPigeon"
YES=0; DELIVER=""; ROUTER=""; SKIP_HERMES_SETUP=0; REFRESH=""
MIN_PY="3.10"

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --dir) DIR="$2"; shift ;;
    --deliver) DELIVER="$2"; shift ;;
    --with-router) ROUTER="--with-router" ;;
    --skip-hermes-setup) SKIP_HERMES_SETUP=1 ;;
    -h|--help) sed -n 2,20p "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || echo "see the header of bootstrap.sh"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- helpers
# When piped from curl, stdin is this script — questions must read the terminal instead.
TTY=""
if (exec </dev/tty) 2>/dev/null; then TTY=/dev/tty; fi   # exists AND can be opened (not in CI/containers)
bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
ask()  {  # ask "question" [default y|n] -> 0 for yes
  local q="$1" def="${2:-y}" a
  if [ "$YES" -eq 1 ]; then return 0; fi
  [ -n "$TTY" ] || return 1                     # nobody to ask: change nothing
  read -r -p "  $q [$([ "$def" = y ] && echo Y/n || echo y/N)] " a <"$TTY" || a=""
  a="${a:-$def}"; [[ "$a" =~ ^[Yy] ]]
}
prompt() {  # prompt "question" default -> echoes answer
  local q="$1" def="$2" a=""
  [ -n "$TTY" ] && read -r -p "  $q [$def] " a <"$TTY" || true
  echo "${a:-$def}"
}
interactive() { [ -n "$TTY" ] && "$@" <"$TTY"; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then have sudo && SUDO="sudo"; fi
export PATH="$HOME/.local/bin:$PATH"          # where the Hermes installer puts hermes + uv

# ---------------------------------------------------------------- 0. platform
OS="$(uname -s)"
case "$OS" in
  Linux)  if grep -qi microsoft /proc/version 2>/dev/null; then PLATFORM=wsl; else PLATFORM=linux; fi ;;
  Darwin) PLATFORM=mac ;;
  MINGW*|MSYS*|CYGWIN*) die "Native Windows isn't supported. Install WSL2 (PowerShell as admin: wsl --install), open Ubuntu, and run this again there." ;;
  *) die "Unsupported system: $OS (Linux, macOS or WSL2 needed)." ;;
esac
bold "TickerPigeon — installer ($PLATFORM)"

# ---------------------------------------------------------------- 1. system packages
bold "1/8 System packages"
py_ok() {  # python3 >= MIN_PY with a working venv module
  have python3 && python3 - "$MIN_PY" <<'PY' 2>/dev/null
import sys, venv, ensurepip  # noqa: F401  (Debian ships python3 without ensurepip until python3-venv)
need = tuple(int(x) for x in sys.argv[1].split("."))
sys.exit(0 if sys.version_info[:2] >= need else 1)
PY
}
missing=()
have git || missing+=(git)
have curl || missing+=(curl)
py_ok || missing+=(python)
if [ ${#missing[@]} -eq 0 ]; then
  ok "git, curl, python3 ≥ $MIN_PY with venv"
else
  warn "missing: ${missing[*]}"
  pkgs=()
  if [ "$PLATFORM" = mac ]; then
    if ! have brew; then
      warn "Homebrew not found. git comes with the Xcode command line tools: xcode-select --install"
      warn "Python will be provided by uv in step 5 if the system one is too old."
    else
      for m in "${missing[@]}"; do case "$m" in python) pkgs+=(python@3.12) ;; *) pkgs+=("$m") ;; esac; done
      ask "Install ${pkgs[*]} with Homebrew?" && brew install "${pkgs[@]}"
    fi
  else
    # DEBIAN_FRONTEND: python3 can pull in tzdata, whose interactive prompt would hang the install
    if   have apt-get; then PM="env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq"; PYPKG="python3 python3-venv python3-pip"; UPD="apt-get update -qq"
    elif have dnf;     then PM="dnf install -y";      PYPKG="python3 python3-pip"
    elif have yum;     then PM="yum install -y";      PYPKG="python3 python3-pip"
    elif have pacman;  then PM="pacman -S --noconfirm --needed"; PYPKG="python python-pip"
    elif have zypper;  then PM="zypper install -y";   PYPKG="python3 python3-pip"
    elif have apk;     then PM="apk add";             PYPKG="python3 py3-pip"
    else PM=""; fi
    # shellcheck disable=SC2206  # PYPKG is a space-separated package list on purpose
    for m in "${missing[@]}"; do case "$m" in python) pkgs+=($PYPKG) ;; *) pkgs+=("$m") ;; esac; done
    if [ -z "$PM" ]; then
      warn "No known package manager — install these yourself: ${missing[*]}"
    elif ask "Install ${pkgs[*]} (needs sudo)?"; then
      [ -n "${UPD:-}" ] && $SUDO $UPD >/dev/null
      $SUDO $PM "${pkgs[@]}"
    fi
  fi
  have git && have curl || die "git and curl are required."
  py_ok && ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')" \
        || warn "python3 ≥ $MIN_PY not available system-wide — will use uv's Python in step 5"
fi

# ---------------------------------------------------------------- 2. the code
bold "2/8 Get the code"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/hermes/install.sh" ] && [ -f "$HERE/pipeline/setup.py" ]; then
  DIR="$HERE"
  ok "using this checkout: $DIR"
elif [ -d "$DIR/.git" ]; then
  before="$(git -C "$DIR" rev-parse HEAD)"
  git -C "$DIR" pull --ff-only --quiet && ok "updated $DIR" || warn "could not update $DIR (local changes?) — using it as is"
  # New code → refresh the copies in ~/.hermes too (old ones are kept as *.bak)
  [ "$(git -C "$DIR" rev-parse HEAD)" != "$before" ] && REFRESH="--force"
else
  git clone --quiet "$REPO_URL" "$DIR" && ok "cloned into $DIR"
fi
cd "$DIR"
[ -f VERSION ] && ok "TickerPigeon $(cat VERSION)"

# ---------------------------------------------------------------- 3. Hermes Agent
bold "3/8 Hermes Agent"
FRESH_HERMES=0
if have hermes; then
  ok "$(hermes --version 2>/dev/null | head -1)"
else
  echo "  Hermes Agent runs the schedules, talks to the AI model and delivers to your chat app."
  echo "  Official installer: https://hermes-agent.nousresearch.com (installs uv, Python 3.11, Node, …)"
  ask "Install Hermes Agent now?" || die "Hermes Agent is required. Install it, then run this again."
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  hash -r
  have hermes || die "hermes is still not on PATH. Open a new terminal and run this script again."
  FRESH_HERMES=1
  ok "installed $(hermes --version 2>/dev/null | head -1)"
fi
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"; export HERMES_HOME

# ---------------------------------------------------------------- 4. Hermes config
bold "4/8 AI provider + chat app"
echo "  The agents use Claude models (Haiku/Sonnet/Opus) — pick Anthropic as the provider (API key or"
echo "  Claude subscription login). For the chat app, Telegram is simplest: create a bot with @BotFather."
if [ "$SKIP_HERMES_SETUP" -eq 1 ]; then
  ok "skipped (--skip-hermes-setup)"
elif [ "$FRESH_HERMES" -eq 1 ] || ask "Run 'hermes setup' to configure the provider and chat app?" n; then
  interactive hermes setup || warn "hermes setup exited early — rerun it any time with: hermes setup"
else
  ok "keeping the current Hermes configuration"
fi

# ---------------------------------------------------------------- 5. project install
bold "5/8 Project install"
if [ ! -x .venv/bin/python ]; then
  if py_ok; then
    python3 -m venv .venv
  else
    have uv || { curl -LsSf https://astral.sh/uv/install.sh | sh; hash -r; }
    uv venv --seed --python 3.11 .venv >/dev/null
  fi
  ok "created .venv ($(.venv/bin/python -c 'import sys;print("Python %d.%d"%sys.version_info[:2])'))"
fi
./hermes/install.sh ${REFRESH:-} $ROUTER

# ---------------------------------------------------------------- 6. time zone
bold "6/8 Time zone"
if [ "$(date +%z)" = "+0800" ]; then
  ok "UTC+08:00 ($(date +%Z)) — schedules run on Malaysia time"
else
  warn "this machine is on $(date +%Z) ($(date +%z)); message times are read as local time"
  if ask "Switch the system time zone to Asia/Kuala_Lumpur (needs sudo)?" n; then
    if [ "$PLATFORM" = mac ]; then $SUDO systemsetup -settimezone Asia/Kuala_Lumpur >/dev/null
    elif have timedatectl && timedatectl >/dev/null 2>&1; then $SUDO timedatectl set-timezone Asia/Kuala_Lumpur
    elif [ -f /usr/share/zoneinfo/Asia/Kuala_Lumpur ]; then $SUDO ln -sf /usr/share/zoneinfo/Asia/Kuala_Lumpur /etc/localtime
    else warn "no time zone database here (install the tzdata package), time zone unchanged"; fi
    [ "$(date +%z)" = "+0800" ] && ok "now $(date +%Z) ($(date +%z))"
  else
    warn "kept — pick your message times in local time during setup"
  fi
fi

# ---------------------------------------------------------------- 7. scheduled jobs + gateway
bold "7/8 Scheduled jobs"
# The chat app is a preference (asked in step 8); --deliver pre-fills it.
if [ -n "$DELIVER" ]; then
  .venv/bin/python -m pipeline.setup set "deliver=$DELIVER" --no-apply >/dev/null && ok "chat app: $DELIVER"
fi
if ! .venv/bin/python -m pipeline.setup target >/dev/null 2>&1; then
  warn "no chat app is connected in Hermes yet (step 4: hermes setup gateway)"
  DELIVER="$(prompt "Deliver to which app for now (telegram, discord, slack, whatsapp, signal, …)?" telegram)"
  .venv/bin/python -m pipeline.setup set "deliver=$DELIVER" --no-apply >/dev/null
fi
./hermes/cron/create-jobs.sh

GW="$(hermes cron status 2>/dev/null || true)"
if grep -qi "is running" <<<"$GW"; then
  ok "gateway is running — jobs will fire on schedule"
elif [ "$PLATFORM" = mac ] || (have systemctl && systemctl --user show-environment >/dev/null 2>&1); then
  if ask "Install the Hermes gateway as a background service so jobs fire on schedule?"; then
    { hermes gateway install && hermes gateway start && ok "gateway service started"; } \
      || warn "gateway service didn't start — check 'hermes gateway status', or run 'hermes gateway run'"
  else
    warn "jobs only fire while the gateway runs: hermes gateway install (service) or hermes gateway run"
  fi
else
  warn "no systemd here, so the gateway can't run as a service. Jobs fire only while it runs:"
  warn "  hermes gateway run        (keep it open, e.g. in tmux)"
  [ "$PLATFORM" = wsl ] && warn "  or enable systemd in WSL: add '[boot] systemd=true' to /etc/wsl.conf, then 'wsl --shutdown'"
fi

# ---------------------------------------------------------------- 8. your newsletter
bold "8/8 Your stocks and messages"
STATUS="$(.venv/bin/python -m pipeline.setup status 2>/dev/null || true)"
if grep -q "^Set up: yes" <<<"$STATUS"; then
  sed -n '3,21p' <<<"$STATUS" | sed 's/^/  /'
  if ask "Run the setup again to change stocks or message times?" n; then
    interactive .venv/bin/python -m pipeline.setup wizard || true
  fi
elif [ -n "$TTY" ]; then
  interactive .venv/bin/python -m pipeline.setup wizard || warn "setup not finished — run it later, or send /setup to the bot"
else
  warn "no terminal to ask questions — run: .venv/bin/python -m pipeline.setup (or send /setup to the bot)"
fi

bold "Done"
cat <<EOF
  Project:   $DIR
  Status:    cd "$DIR" && .venv/bin/python -m pipeline.setup status
  Change:    send /setup to your bot, or run .venv/bin/python -m pipeline.setup
  Test now:  .venv/bin/python -m pipeline.scan --dry-run      (what would alert right now)
  Jobs:      hermes cron list
EOF
