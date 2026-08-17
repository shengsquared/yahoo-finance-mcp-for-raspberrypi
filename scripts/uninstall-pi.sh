#!/usr/bin/env bash
# Remove the Yahoo Finance MCP server from this Raspberry Pi. Reverses whichever
# of scripts/install-pi.sh (systemd) or `docker compose up` (Docker) was used --
# checks for both and removes whatever it finds. Safe to run if neither was used.
#
# Usage:
#   sudo bash scripts/uninstall-pi.sh
#
# Environment overrides:
#   APP_DIR   installation directory to remove (default /opt/yahoo-finance-mcp)
#   FORCE=1   skip the confirmation prompt

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/yahoo-finance-mcp}"
SERVICE_NAME="yahoo-finance-mcp.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
CREDENTIALS_FILE="/etc/yahoo-finance-mcp.env"
STATE_DIR="/var/lib/yahoo-finance-mcp"
FORCE="${FORCE:-0}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script needs root to remove ${APP_DIR}, the systemd unit, and /etc/systemd/system." >&2
    echo "Re-run it as: sudo $0" >&2
    exit 1
fi

echo "This will remove, if present:"
echo "  - the systemd service (${SERVICE_NAME}) and its unit file"
echo "  - ${APP_DIR} (the virtualenv/install)"
echo "  - ${CREDENTIALS_FILE} (client credentials, if you set any)"
echo "  - ${STATE_DIR} (the yfinance cache under systemd's StateDirectory)"
echo "  - the yahoo-finance-mcp Docker container/image/cache volume, if you used Docker instead"
echo

if [[ "${FORCE}" != "1" ]]; then
    read -r -p "Continue? [y/N] " reply
    [[ "${reply}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

did_something=0

# --- systemd install (scripts/install-pi.sh) ---
if [[ -f "${UNIT_PATH}" ]]; then
    echo "==> Stopping and disabling ${SERVICE_NAME}"
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${UNIT_PATH}"
    systemctl daemon-reload
    systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
    did_something=1
fi

if [[ -d "${APP_DIR}" ]]; then
    echo "==> Removing ${APP_DIR}"
    rm -rf "${APP_DIR}"
    did_something=1
fi

if [[ -f "${CREDENTIALS_FILE}" ]]; then
    echo "==> Removing ${CREDENTIALS_FILE}"
    rm -f "${CREDENTIALS_FILE}"
    did_something=1
fi

if [[ -d "${STATE_DIR}" ]]; then
    echo "==> Removing ${STATE_DIR}"
    rm -rf "${STATE_DIR}"
    did_something=1
fi

# --- Docker install (docker compose up) ---
if command -v docker >/dev/null 2>&1; then
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx yahoo-finance-mcp; then
        echo "==> Stopping and removing the yahoo-finance-mcp container"
        docker rm -f yahoo-finance-mcp >/dev/null
        did_something=1
    fi

    if docker image inspect yahoo-finance-mcp:local >/dev/null 2>&1; then
        echo "==> Removing the yahoo-finance-mcp:local image"
        docker rmi yahoo-finance-mcp:local >/dev/null
        did_something=1
    fi

    # The compose file doesn't pin the cache volume's full name, so it's prefixed
    # with whatever project name `docker compose` picked (usually the directory
    # name) -- match on the volume's own name instead of guessing that prefix.
    for vol in $(docker volume ls -q --filter name=yfinance-cache 2>/dev/null); do
        echo "==> Removing Docker volume ${vol}"
        docker volume rm "${vol}" >/dev/null
        did_something=1
    done
fi

echo
if [[ "${did_something}" -eq 1 ]]; then
    echo "==> Done. The server has been removed."
    echo "    If you also set up cloudflared or Tailscale Funnel for this, stop those separately:"
    echo "      sudo systemctl disable --now cloudflared   # Cloudflare Tunnel, if installed as a service"
    echo "      sudo tailscale funnel off                  # Tailscale Funnel"
else
    echo "==> Nothing found to remove (no systemd unit, install directory, or Docker container)."
fi
