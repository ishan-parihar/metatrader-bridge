#!/usr/bin/env bash
set -euo pipefail

# Install MetaTraderBridge user-level systemd services (no sudo needed)
# Usage: ./install-local.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$USER_DIR"

SERVICES=(metatrader-bridge-mt5-terminal metatrader-bridge-mt5-tcp-bridge metatrader-bridge-mt5-http-gateway metatrader-bridge-mt5-mcp-server)

info "Stopping existing user services..."
for svc in "${SERVICES[@]}"; do
    systemctl --user stop "${svc}.service" 2>/dev/null || true
done

info "Installing service files to $USER_DIR..."
for svc in "${SERVICES[@]}"; do
    src="${SCRIPT_DIR}/${svc}.service"
    dst="${USER_DIR}/${svc}.service"
    if [[ ! -f "$src" ]]; then
        error "Missing: $src"
        exit 1
    fi
    cp "$src" "$dst"
    success "Installed ${svc}.service"
done

info "Reloading user systemd daemon..."
systemctl --user daemon-reload

info "Enabling services for autorun..."
for svc in "${SERVICES[@]}"; do
    systemctl --user enable "${svc}.service"
    success "Enabled ${svc}"
done

echo
info "Services installed. Start with:"
info "  systemctl --user start mt5-terminal"
info "  systemctl --user start mt5-tcp-bridge mt5-http-gateway mt5-mcp-server"
