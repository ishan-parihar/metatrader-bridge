#!/usr/bin/env bash
set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }

# Require root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (sudo $0)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="/opt/MetaTraderBridge"
SERVICE_USER="mt5-bridge"
SERVICE_GROUP="mt5-bridge"

SERVICES=(
    "metatrader-bridge-mt5-terminal"
    "metatrader-bridge-mt5-tcp-bridge"
    "metatrader-bridge-mt5-http-gateway"
    "metatrader-bridge-mt5-mcp-server"
)

info "========================================="
info "  MetaTraderBridge Systemd Services Installer"
info "========================================="
echo

# Step 1: Create service user/group
info "Step 1: Creating service user/group..."
if id "$SERVICE_USER" &>/dev/null; then
    warn "User '$SERVICE_USER' already exists, skipping"
else
    groupadd --system "$SERVICE_GROUP"
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$PROJECT_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    success "Created user '$SERVICE_USER' and group '$SERVICE_GROUP'"
fi
echo

# Step 2: Verify project directory
info "Step 2: Checking project directory..."
if [[ ! -d "$PROJECT_DIR" ]]; then
    error "Project directory '$PROJECT_DIR' does not exist."
    error "Clone the repository first:"
    error "  git clone <repo-url> $PROJECT_DIR"
    exit 1
fi

# Verify venv exists
if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
    warn "Virtual environment not found at $PROJECT_DIR/.venv"
    info "Creating virtual environment..."
    cd "$PROJECT_DIR"
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q poetry
    .venv/bin/poetry install --no-interaction --without dev
    success "Virtual environment created and dependencies installed"
fi

# Set ownership
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$PROJECT_DIR"
success "Project directory ownership set to $SERVICE_USER:$SERVICE_GROUP"
echo

# Step 3: Install health check script
info "Step 3: Installing MT5 health check script..."
HEALTH_CHECK_SRC="${SCRIPT_DIR}/mt5-terminal-health-check.sh"
HEALTH_CHECK_DST="$PROJECT_DIR/deploy/systemd/mt5-terminal-health-check.sh"
if [[ ! -f "$HEALTH_CHECK_SRC" ]]; then
    error "Health check script not found: $HEALTH_CHECK_SRC"
    exit 1
fi
cp "$HEALTH_CHECK_SRC" "$HEALTH_CHECK_DST"
chmod +x "$HEALTH_CHECK_DST"
success "Installed MT5 health check script"
echo

# Step 4: Install systemd service files
info "Step 4: Installing systemd service files..."
for svc in "${SERVICES[@]}"; do
    src="${SCRIPT_DIR}/${svc}.service"
    dst="/etc/systemd/system/${svc}.service"

    if [[ ! -f "$src" ]]; then
        error "Service file not found: $src"
        exit 1
    fi

    cp "$src" "$dst"
    chmod 644 "$dst"
    success "Installed ${svc}.service"
done
echo

# Step 4: Reload systemd daemon
info "Step 4: Reloading systemd daemon..."
systemctl daemon-reload
success "Systemd daemon reloaded"
echo

# Step 5: Enable and start services
info "Step 5: Enabling and starting services..."
for svc in "${SERVICES[@]}"; do
    info "Enabling ${svc}..."
    systemctl enable "${svc}.service"

    info "Starting ${svc}..."
    if systemctl start "${svc}.service"; then
        success "${svc} started successfully"
    else
        error "Failed to start ${svc}"
        warn "Check logs with: journalctl -u ${svc} -e"
    fi
    echo
done

# Step 6: Verify service status
info "Step 6: Service status check..."
echo
for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}.service"; then
        success "${svc} is running"
    else
        error "${svc} is NOT running"
        warn "Check logs: journalctl -u ${svc} -e --no-pager"
    fi
done
echo

# Summary
info "========================================="
info "  Installation Complete"
info "========================================="
echo
info "Services:"
info "  MT5 Health:    systemctl status mt5-terminal-health-check"
info "  MT5 Terminal:  systemctl status mt5-terminal"
info "  TCP Bridge:    systemctl status mt5-tcp-bridge"
info "  HTTP Gateway:  systemctl status mt5-http-gateway"
info "  MCP Server:    systemctl status mt5-mcp-server"
echo
info "Logs:"
info "  MT5 Health:    journalctl -u mt5-terminal-health-check -f"
info "  TCP Bridge:    journalctl -u mt5-tcp-bridge -f"
info "  HTTP Gateway:  journalctl -u mt5-http-gateway -f"
info "  MCP Server:    journalctl -u mt5-mcp-server -f"
echo
info "Health Checks:"
info "  TCP Bridge:    curl -s http://127.0.0.1:8025/status"
info "  HTTP Gateway:  curl -s http://127.0.0.1:8020/bridge/health"
info "  MCP Server:    curl -s http://127.0.0.1:8010/health"
echo
