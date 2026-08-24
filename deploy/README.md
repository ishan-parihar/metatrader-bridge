# MetaTraderBridge Deployment Guide

Production deployment files for the 3-process MetaTraderBridge architecture.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  MCP Server │────▶│  HTTP Gateway    │────▶│  TCP Bridge  │
│  :8010      │     │  :8020           │     │  :8025       │
└─────────────┘     └──────────────────┘     └──────┬───────┘
                                                    │
                                          ┌─────────▼────────┐
                                          │  MQL5 EA (MT5)   │
                                          │  Wine / Bottles  │
                                          └──────────────────┘
```

| Service | Port | Purpose |
|---------|------|---------|
| MCP Server | 8010 | AI agent interface (FastAPI) |
| HTTP Gateway | 8020 | Command queue, HTTP fallback, metrics |
| TCP Bridge | 8025 | Low-latency push to MQL5 EA |

## Docker Compose Quick Start

### Prerequisites

- Docker Engine 24+ and Docker Compose v2
- At least 1GB RAM available for all services

### Start All Services

```bash
cd deploy
docker compose up -d
```

### View Logs

```bash
docker compose logs -f
docker compose logs -f tcp-bridge
docker compose logs -f http-gateway
docker compose logs -f mcp-server
```

### Stop Services

```bash
docker compose down
docker compose down -v  # Also removes Redis data
```

### Restart Single Service

```bash
docker compose restart mcp-server
docker compose up -d --force-recreate http-gateway
```

## Systemd Installation

### Prerequisites

- Linux system with systemd (Ubuntu 22.04+, Debian 12+, RHEL 9+)
- Python 3.11+
- Project cloned to `/opt/MetaTraderBridge`

### Install

```bash
# Clone to target directory
sudo git clone <repo-url> /opt/MetaTraderBridge
cd /opt/MetaTraderBridge

# Install dependencies
python3 -m venv .venv
.venv/bin/pip install --upgrade pip poetry
.venv/bin/poetry install --no-interaction --without dev

# Run installer
sudo deploy/systemd/install.sh
```

### Manage Services

```bash
# Check status
systemctl status mt5-tcp-bridge
systemctl status mt5-http-gateway
systemctl status MetaTraderBridge-server

# Restart
sudo systemctl restart mt5-tcp-bridge

# View logs
journalctl -u mt5-tcp-bridge -f
journalctl -u mt5-http-gateway -f
journalctl -u MetaTraderBridge-server -f
```

### Upgrade

```bash
cd /opt/MetaTraderBridge
sudo git pull
.venv/bin/poetry install --no-interaction --without dev

sudo systemctl restart mt5-tcp-bridge mt5-http-gateway MetaTraderBridge-server
```

## Environment Variables

### TCP Bridge (port 8025)

| Variable | Default | Description |
|----------|---------|-------------|
| `MT5_TCP_BRIDGE_PORT` | `8025` | TCP listen port |
| `MT5_TCP_BRIDGE_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in Docker) |
| `MT5_REDIS_URL` | - | Redis URL for command persistence (optional) |

### HTTP Gateway (port 8020)

| Variable | Default | Description |
|----------|---------|-------------|
| `MT5_GATEWAY_URL` | `http://127.0.0.1:8020` | Gateway endpoint URL |
| `MT5_REDIS_URL` | - | Redis URL (optional, falls back to in-memory) |
| `MT5_TCP_BRIDGE_ENABLED` | `true` | Enable TCP bridge integration |
| `MT5_TCP_BRIDGE_PORT` | `8025` | TCP bridge port for direct forwarding |
| `MT5_COMMAND_SECRET` | - | Secret for command authorization (optional) |

### MCP Server (port 8010)

| Variable | Default | Description |
|----------|---------|-------------|
| `MT5_GATEWAY_URL` | `http://127.0.0.1:8020` | Gateway endpoint for routing commands |
| `MT5_REDIS_URL` | - | Redis URL (optional) |
| `MT5_TCP_BRIDGE_ENABLED` | `true` | Use TCP bridge for low-latency execution |
| `MT5_TCP_BRIDGE_PORT` | `8025` | TCP bridge port |

### Docker Compose Overrides

Create `deploy/.env` to override ports:

```bash
MT5_TCP_BRIDGE_PORT=8025
MT5_GATEWAY_PORT=8020
MT5_MCP_SERVER_PORT=8010
MT5_REDIS_PORT=6379
```

## Health Check Endpoints

| Service | Endpoint | Expected Response |
|---------|----------|-------------------|
| TCP Bridge | `http://127.0.0.1:8025/status` | JSON status |
| HTTP Gateway | `http://127.0.0.1:8020/bridge/health` | `{"status": "ok"}` |
| MCP Server | `http://127.0.0.1:8010/health` | Health status |

Quick health check:

```bash
curl -s http://127.0.0.1:8025/status && echo " TCP Bridge OK"
curl -s http://127.0.0.1:8020/bridge/health && echo " HTTP Gateway OK"
curl -s http://127.0.0.1:8010/health && echo " MCP Server OK"
```

## Log Locations

### Docker

```bash
docker compose logs tcp-bridge
docker compose logs http-gateway
docker compose logs mcp-server
docker compose logs redis
```

### Systemd

Logs go to journald, accessible via:

```bash
journalctl -u mt5-tcp-bridge --since "1 hour ago"
journalctl -u mt5-http-gateway -e --no-pager
journalctl -u MetaTraderBridge-server -f
```

### Application Log Files

When running directly (not via systemd/Docker), logs write to project root:

- `tcp_bridge_8025.log`
- `bridge_gateway_8020.log`
- `mcp_server_8010.log`

## Troubleshooting

### TCP Bridge Not Starting

```bash
# Check if port 8025 is in use
ss -tlnp | grep 8025

# Check logs
journalctl -u mt5-tcp-bridge -e
docker compose logs tcp-bridge
```

### HTTP Gateway Health Check Failing

```bash
# Verify gateway is listening
curl -v http://127.0.0.1:8020/bridge/health

# Check Redis connectivity (if using Redis)
curl -s http://127.0.0.1:8020/bridge/terminal/status
```

### MCP Server Cannot Reach Gateway

```bash
# From MCP server container (Docker)
docker exec MetaTraderBridge-server curl -s http://http-gateway:8020/bridge/health

# On bare metal
curl -s http://127.0.0.1:8020/bridge/health
```

### Service Fails to Start After Upgrade

```bash
# Reinstall dependencies
cd /opt/MetaTraderBridge
.venv/bin/poetry install --no-interaction --without dev

# Restart
sudo systemctl restart mt5-tcp-bridge mt5-http-gateway MetaTraderBridge-server
```

### Permission Denied Errors

```bash
# Fix ownership
sudo chown -R mt5-bridge:mt5-bridge /opt/MetaTraderBridge
```

## Resource Usage

| Service | Memory (typical) | CPU (idle) |
|---------|-----------------|------------|
| Redis | 64 MB | ~0% |
| TCP Bridge | 128 MB | ~0% |
| HTTP Gateway | 128 MB | ~0% |
| MCP Server | 128 MB | ~0% |

Total: ~450 MB RAM at idle. Docker compose file includes resource limits that can be adjusted.
