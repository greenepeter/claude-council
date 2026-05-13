#!/usr/bin/env bash
# scripts/run_autonomous.sh
#
# Cron wrapper around run_autonomous.py. Handles:
#   - venv activation (so the right Python + deps are used)
#   - PYTHONPATH so `trade_council` is importable
#   - .env loading (the Python script also loads it via dotenv, but we set it
#     here too so any subprocess call sees the key)
#   - logging to runtime/cron.log
#
# Cron invokes this hourly. Any flags passed are forwarded to run_autonomous.py
# (e.g. --dry-run).

set -euo pipefail

# Resolve paths relative to this script regardless of where cron invokes it from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"

VENV_BIN="$APP_DIR/.venv/bin"
if [[ ! -x "$VENV_BIN/python" ]]; then
    echo "[run_autonomous.sh] venv missing at $APP_DIR/.venv — run vps_bootstrap.sh first" >&2
    exit 1
fi

# Make trade_council importable.
export PYTHONPATH="$APP_DIR/src:${PYTHONPATH:-}"

# Source the venv's activate so any 'python' inside the script also works.
# shellcheck disable=SC1091
source "$APP_DIR/.venv/bin/activate"

# Belt-and-suspenders: also export ANTHROPIC_API_KEY from .env so any
# subprocesses (e.g. claude -p if claude_code mode is used) see it.
if [[ -f "$APP_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "$APP_DIR/.env"
    set +a
fi

cd "$APP_DIR"

# Print a header to make multi-run logs scannable.
echo ""
echo "=================================================================="
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run_autonomous starting (args: $*)"
echo "=================================================================="

exec python scripts/run_autonomous.py "$@"
