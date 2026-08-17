#!/usr/bin/env bash
# Install the Yahoo Finance MCP server as a systemd service on a Raspberry Pi.
#
# Usage (from a clone of this repository):
#   sudo bash scripts/install-pi.sh
#
# Environment overrides:
#   APP_DIR   installation directory        (default /opt/yahoo-finance-mcp)
#   MCP_HOST  interface to bind             (default 0.0.0.0)
#   MCP_PORT  port to listen on             (default 8000)

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/yahoo-finance-mcp}"
MCP_HOST="${MCP_HOST:-0.0.0.0}"
MCP_PORT="${MCP_PORT:-8000}"
SERVICE_NAME="yahoo-finance-mcp.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script needs root to write to ${APP_DIR} and /etc/systemd/system." >&2
    echo "Re-run it as: sudo $0" >&2
    exit 1
fi

arch="$(uname -m)"
case "${arch}" in
    aarch64|arm64|x86_64)
        ;;
    armv7l|armv6l)
        cat >&2 <<'EOF'
Warning: this is a 32-bit ARM userland. PyPI has no numpy/pandas wheels for it,
so the install below will either pull them from piwheels (Raspberry Pi OS only,
and can be slow) or compile them from source, which takes a long time and needs
build tools plus extra swap. A 64-bit Raspberry Pi OS install is strongly
recommended -- see docs/raspberry-pi.md.
EOF
        read -r -p "Continue anyway? [y/N] " reply
        [[ "${reply}" =~ ^[Yy]$ ]] || exit 1
        ;;
    *)
        echo "Warning: unrecognised architecture '${arch}', continuing anyway." >&2
        ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it with: sudo apt install -y python3 python3-venv" >&2
    exit 1
fi

python3 - <<'EOF' || { echo "Python 3.11 or newer is required (see docs/raspberry-pi.md)." >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF

if ! python3 -c "import venv" >/dev/null 2>&1; then
    echo "The venv module is missing. Install it with: sudo apt install -y python3-venv" >&2
    exit 1
fi

echo "==> Installing to ${APP_DIR}"
install -d -m 0755 "${APP_DIR}"

echo "==> Creating virtual environment"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel

echo "==> Installing the server (this can take a few minutes on a Pi)"
"${APP_DIR}/.venv/bin/pip" install "${REPO_DIR}"

echo "==> Installing ${SERVICE_NAME}"
sed \
    -e "s|/opt/yahoo-finance-mcp|${APP_DIR}|g" \
    -e "s|^Environment=YFINANCE_MCP_HOST=.*|Environment=YFINANCE_MCP_HOST=${MCP_HOST}|" \
    -e "s|^Environment=YFINANCE_MCP_PORT=.*|Environment=YFINANCE_MCP_PORT=${MCP_PORT}|" \
    "${REPO_DIR}/deploy/${SERVICE_NAME}" >"/etc/systemd/system/${SERVICE_NAME}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo
echo "==> Done. The server is listening on http://${MCP_HOST}:${MCP_PORT}/mcp"
echo "    Status: systemctl status ${SERVICE_NAME}"
echo "    Logs:   journalctl -u ${SERVICE_NAME} -f"
