#!/bin/bash
# MetaTraderBridge Server Startup Script
# Starts Bridge Gateway (8020) and MCP Server (8010) in correct order

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$PROJECT_ROOT/src:$PYTHONPATH"

cd "$PROJECT_ROOT"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Cleanup function
cleanup() {
    log_info "Shutting down servers..."
    if [ ! -z "$GATEWAY_PID" ]; then
        kill $GATEWAY_PID 2>/dev/null || true
    fi
    if [ ! -z "$MCP_PID" ]; then
        kill $MCP_PID 2>/dev/null || true
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

# Check if ports are already in use
check_port() {
    if netstat -tuln 2>/dev/null | grep -q ":$1 " || ss -tuln 2>/dev/null | grep -q ":$1 "; then
        return 0
    fi
    return 1
}

# Kill existing processes on ports
kill_on_port() {
    local port=$1
    local pid=$(lsof -t -i:$port 2>/dev/null)
    if [ ! -z "$pid" ]; then
        log_warn "Killing process on port $port (PID: $pid)"
        kill $pid 2>/dev/null || true
        sleep 1
    fi
}

# Main startup sequence
main() {
    log_info "Starting MetaTraderBridge servers..."
    
    # Kill any existing processes on our ports
    kill_on_port 8020
    kill_on_port 8010
    
    # Start Bridge Gateway first (port 8020)
    log_info "Starting Bridge Gateway on port 8020..."
    python apps/bridge_gateway/main.py > bridge_gateway.log 2>&1 &
    GATEWAY_PID=$!
    
    # Wait for gateway to start
    sleep 2
    
    # Check if gateway started successfully
    if ! kill -0 $GATEWAY_PID 2>/dev/null; then
        log_error "Bridge Gateway failed to start. Check bridge_gateway.log"
        cat bridge_gateway.log
        exit 1
    fi
    
    log_info "Bridge Gateway started (PID: $GATEWAY_PID)"
    
    # Start MCP Server (port 8010)
    log_info "Starting MCP Server on port 8010..."
    python apps/mcp_server/main.py > mcp_server.log 2>&1 &
    MCP_PID=$!
    
    # Wait for MCP server to start
    sleep 2
    
    # Check if MCP server started successfully
    if ! kill -0 $MCP_PID 2>/dev/null; then
        log_error "MCP Server failed to start. Check mcp_server.log"
        cat mcp_server.log
        kill $GATEWAY_PID 2>/dev/null || true
        exit 1
    fi
    
    log_info "MCP Server started (PID: $MCP_PID)"
    
    # Verify both servers are responding
    sleep 1
    
    log_info "Testing server health..."
    
    # Test gateway health
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8020/bridge/terminal/status | grep -q "200\|401"; then
        log_info "Bridge Gateway: OK"
    else
        log_warn "Bridge Gateway: Not responding (may need EA heartbeat)"
    fi
    
    # Test MCP server health
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8010/health | grep -q "200"; then
        log_info "MCP Server: OK"
    else
        log_error "MCP Server: Not responding"
        exit 1
    fi
    
    log_info "All servers started successfully!"
    log_info "Bridge Gateway: http://127.0.0.1:8020"
    log_info "MCP Server: http://127.0.0.1:8010"
    log_info ""
    log_info "Press Ctrl+C to stop all servers"
    
    # Keep running
    wait
}

main "$@"
