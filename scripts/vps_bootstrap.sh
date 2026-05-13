#!/usr/bin/env bash
# scripts/vps_bootstrap.sh
#
# One-shot VPS bootstrap for claude_council v2 (trade_council). Run ONCE as
# root on a fresh Ubuntu 24.04 box after the user has done the baseline
# (apt update, install python3 / git / venv / sqlite3, create trader user).
#
# Usage:
#   sudo bash scripts/vps_bootstrap.sh
#
# Or first-time, before the repo is cloned, pipe directly from GitHub raw:
#   curl -fsSL https://raw.githubusercontent.com/greenepeter/claude-council/main/scripts/vps_bootstrap.sh | sudo bash
#
# What it does (idempotent — safe to re-run):
#   1. Sanity-checks the environment (root, trader user exists, baseline pkgs)
#   2. Clones the repo to /home/trader/trade_council (or pulls if exists)
#   3. Creates a Python venv and installs requirements
#   4. Scaffolds .env from .env.example (you fill in the real API key)
#   5. Creates a KILL file so cron is no-op until you remove it (safety)
#   6. Installs a crontab entry that runs run_autonomous.sh hourly at :17
#
# After this script finishes:
#   - Edit /home/trader/trade_council/.env and paste your real ANTHROPIC_API_KEY
#   - Decide on backend mode (claude_code requires `claude login` separately;
#     api mode works as soon as the key is set)
#   - When ready, remove the KILL file: sudo -u trader rm /home/trader/trade_council/KILL
#   - Cron starts firing at the next :17 past the hour

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/greenepeter/claude-council.git"
TRADER_USER="trader"
APP_DIR="/home/${TRADER_USER}/trade_council"
VENV_DIR="${APP_DIR}/.venv"
CRON_MINUTE="17"   # fire at :17 past every hour (off-peak from most crons)
LOG_PREFIX="\033[1;32m[bootstrap]\033[0m"
ERR_PREFIX="\033[1;31m[bootstrap ERROR]\033[0m"

log() { echo -e "${LOG_PREFIX} $*"; }
err() { echo -e "${ERR_PREFIX} $*" >&2; }

# ── Sanity checks ─────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Must run as root. Try: sudo bash $0"
    exit 1
fi

if ! id "$TRADER_USER" &>/dev/null; then
    err "User '$TRADER_USER' does not exist."
    err "Run 'adduser $TRADER_USER' first, then re-run this script."
    exit 1
fi

for cmd in python3 git; do
    if ! command -v "$cmd" &>/dev/null; then
        err "Required command '$cmd' not found."
        err "Run baseline first: apt update && apt install -y python3 python3-venv python3-pip git sqlite3"
        exit 1
    fi
done

if ! python3 -c "import venv" &>/dev/null; then
    err "python3-venv module not available."
    err "Run: apt install -y python3-venv"
    exit 1
fi

log "Sanity checks passed."

# ── Clone or update the repo ──────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
    log "Repo already at $APP_DIR — pulling latest"
    sudo -u "$TRADER_USER" git -C "$APP_DIR" fetch --all --prune
    sudo -u "$TRADER_USER" git -C "$APP_DIR" pull --ff-only
else
    log "Cloning $REPO_URL into $APP_DIR"
    sudo -u "$TRADER_USER" git clone "$REPO_URL" "$APP_DIR"
fi

# Ensure trader owns the whole tree (in case git was run as root somewhere).
chown -R "${TRADER_USER}:${TRADER_USER}" "$APP_DIR"

# ── Python venv + dependencies ────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log "Creating venv at $VENV_DIR"
    sudo -u "$TRADER_USER" python3 -m venv "$VENV_DIR"
else
    log "venv already exists at $VENV_DIR"
fi

log "Upgrading pip"
sudo -u "$TRADER_USER" "$VENV_DIR/bin/pip" install --quiet --upgrade pip

if [[ -f "$APP_DIR/requirements.txt" ]]; then
    log "Installing requirements.txt"
    sudo -u "$TRADER_USER" "$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
else
    err "No requirements.txt found — skipping pip install."
    err "If this is unexpected, check the repo contents."
fi

# ── .env scaffold ─────────────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
    if [[ -f "$APP_DIR/.env.example" ]]; then
        log "Scaffolding .env from .env.example"
        sudo -u "$TRADER_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        chmod 600 "$APP_DIR/.env"
        chown "${TRADER_USER}:${TRADER_USER}" "$APP_DIR/.env"
    else
        err "No .env.example to copy from. You'll need to create .env manually."
    fi
else
    log ".env already exists — leaving as-is"
fi

# ── Runtime directories ───────────────────────────────────────────────────
log "Ensuring runtime/ debates/ trade_plans/ exist"
sudo -u "$TRADER_USER" mkdir -p "$APP_DIR/runtime" "$APP_DIR/debates" "$APP_DIR/trade_plans"

# ── Make the wrapper executable ───────────────────────────────────────────
if [[ -f "$APP_DIR/scripts/run_autonomous.sh" ]]; then
    chmod +x "$APP_DIR/scripts/run_autonomous.sh"
else
    err "Missing scripts/run_autonomous.sh — cron will fail until it's added."
fi

# ── Safety: KILL file so cron is no-op until you remove it ────────────────
KILL_FILE="$APP_DIR/KILL"
if [[ ! -f "$KILL_FILE" ]]; then
    log "Creating KILL file for safety (cron will skip until you remove it)"
    sudo -u "$TRADER_USER" touch "$KILL_FILE"
fi

# ── Crontab entry ─────────────────────────────────────────────────────────
CRON_LINE="${CRON_MINUTE} * * * * ${APP_DIR}/scripts/run_autonomous.sh >> ${APP_DIR}/runtime/cron.log 2>&1"

log "Installing crontab for ${TRADER_USER} (fires at :${CRON_MINUTE} every hour)"
# Drop any existing run_autonomous.sh entry, append fresh.
(
    sudo -u "$TRADER_USER" crontab -l 2>/dev/null | grep -v "run_autonomous.sh" || true
    echo "$CRON_LINE"
) | sudo -u "$TRADER_USER" crontab -

# ── Done ──────────────────────────────────────────────────────────────────
echo ""
log "Bootstrap complete."
echo ""
log "Next steps:"
log "  1) Put your real API key in:    ${APP_DIR}/.env"
log "     (vim/nano as trader, replace 'sk-ant-PASTE_YOUR_KEY_HERE')"
log ""
log "  2) Dry-run smoke test:"
log "       sudo -u ${TRADER_USER} ${APP_DIR}/scripts/run_autonomous.sh --dry-run"
log ""
log "  3) When confident, remove the KILL file so cron starts firing:"
log "       sudo -u ${TRADER_USER} rm ${KILL_FILE}"
log ""
log "  4) Tail the log to watch the first real run:"
log "       tail -f ${APP_DIR}/runtime/cron.log"
log ""
log "  5) If you want claude_code mode (subscription) instead of api mode,"
log "     you'll also need to install Node.js + the claude CLI and run"
log "     'claude login' as the trader user (one-time interactive setup)."
