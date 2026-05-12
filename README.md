# TradeBridge

A production-ready bridge between MetaTrader 5 and Model Context Protocol (MCP), enabling AI-driven trading analysis and execution on Linux systems. Runs as a single combined container or a full stack with a headless MT5 terminal in Docker.

**Resource footprint:** ~68 MB for Python services (combined), ~210 MB with MT5 terminal. Fits easily on a 512 MB VPS.

**MCP Tools:** 91 curated, non-redundant MCP tools across 14 modules + WebSocket streaming for real-time data.

## For AI Agents

**Start here:** `~/.agents/skills/mt5-trading/SKILL.md` — Complete trading workflow, tool usage guide, polling protocol, and decision framework.

The SKILL.md contains everything an AI trading agent needs:
- 12-phase trading cycle (State Triage → Continuous Cycling)
- Tool availability and usage patterns
- Polling tiers and wait protocols
- Risk management and position sizing
- Analysis pipeline with fallback behavior
- Metacognition and decision journaling

## Architecture

```
                                           ┌─────────────────────────────┐
                                           │       AI Agent(s)           │
                                           │  MCP Client / WebSocket    │
                                           └────────────┬────────────────┘
                                                        │
                                               MCP SSE │ WS │ REST
                                                        │
                                                        ▼
        ┌───────────────────────────────────────────────────┐
        │         TradeBridge Unified Server (:8010)        │
        │                                                   │
        │  HTTP/MCP Server                                  │
        │  ├── FastMCP SSE (/mcp) — 91 MCP tools            │
        │  ├── REST endpoints (/tools/*)                    │
        │  ├── WebSocket streaming (/ws/prices)             │
        │  ├── Gateway routes (/bridge/*)                   │
        │  ├── Health (/health)                             │
        │  └── Metrics (/metrics)                           │
        │                                                   │
        │  TCP Bridge (:8025) — Persistent EA connection    │
        └──────────────────────┬────────────────────────────┘
                               │ TCP (:8025)
                               ▼
                   ┌──────────────────────────────┐
                   │   MT5 Terminal (1 container)  │
                   │   WineHQ 11 + Xvfb + EA      │
                   │   ~210 MB RSS                │
                   └──────────────────────────────┘
                      │ TCP (:8025)
                      ▼
          ┌──────────────────────────────┐
          │   MT5 Terminal (1 container) │
          │   WineHQ 11 + Xvfb + EA     │
          │   ~210 MB RSS               │
          └──────────────────────────────┘
```

**Latency:** ~15-25ms end-to-end via in-process bridge (no network hop between services)

**Key Design:**
- **Linux-Compatible**: MQL5 EA bridge (no Python MT5 module)
- **Unified Process**: Single Python process serves all HTTP + MCP + TCP ports
- **In-Process Bridge**: TCP bridge embedded in MCP server — zero network overhead for tool calls
- **WebSocket Streaming**: Real-time market data push to subscribed clients via `/ws/prices`
- **TCP Push Model**: Persistent TCP connection, instant command delivery
- **HTTP Fallback**: Automatic if TCP unavailable (via HTTP Gateway routes on same :8010)
- **Demo-First**: Execution tools gated for demo accounts in R&D mode

## Quick Start — Docker (Recommended)

### Prerequisites

- Docker and Docker Compose
- MT5 broker account credentials (optional, for trading)

### Single-container TradeBridge (Python services only)

```bash
# Build the combined image
docker build -f deploy/Dockerfile --target combined-target -t tradebridge:latest .

# Start with Redis
docker compose -f deploy/docker-compose.full.yml up -d redis tradebridge

# Verify health
curl http://localhost:8010/health
# → {"status":"healthy","bridge_connected":false,"tcp_bridge_connected":false,...}
```

### Full stack (with MT5 terminal)

```bash
# Build both images
docker build -f deploy/Dockerfile --target combined-target -t tradebridge:latest .
docker build -f deploy/mt5-terminal/Dockerfile -t mt5-terminal:latest .

# Set broker credentials (optional)
export MT5_BROKER_LOGIN="your_account"
export MT5_BROKER_PASSWORD="your_password"
export MT5_BROKER_SERVER="YourBroker-Server"

# Start everything
docker compose -f deploy/docker-compose.full.yml up -d

# Monitor MT5 terminal setup (first run: ~2-3 min for Wine Mono + binary copy)
docker logs mt5-terminal --tail 20 -f
```

### Resource Usage (measured)

| Service | RSS | Container | Purpose |
|---------|-----|-----------|---------|
| TradeBridge | **64 MB** | 1 container | HTTP/MCP (:8010) + TCP bridge (:8025) |
| MT5 Terminal | **210 MB** | 1 container | WineHQ 11 + Xvfb + MT5 EA |
| Redis | **9 MB** | 1 container | Message queue / cache |
| **Total** | **~283 MB** | **3 containers** | |

### Deploy to VPS

```bash
# Clone and build
git clone git@github.com:ishan-parihar/TradeBridge.git /opt/TradeBridge
cd /opt/TradeBridge
docker build -f deploy/Dockerfile --target combined-target -t tradebridge:latest .
docker compose -f deploy/docker-compose.full.yml up -d redis tradebridge

# Register with Hermes (MCP server manager)
# ~/.hermes/config.yaml:
# mcp_servers:
#   tradebridge:
#     type: http
#     url: http://localhost:8010
#     description: "MT5 Trading Bridge — positions, orders, analysis, market data"
```

## Quick Start — Systemd (for local dev with Bottles)

Requires Python 3.11+, Poetry, and MT5 under Wine/Bottles.

```bash
git clone https://github.com/ishanp321/TradeBridge.git
cd TradeBridge
poetry install

# Single unified process (all services on ports 8010 + 8025):
poetry run python -m apps.bridge.main

# Or install as systemd service:
sudo cp deploy/systemd/tradebridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradebridge.service
sudo systemctl start tradebridge.service
```

### EA Setup (for Bottles/Wine)

1. Compile `ea/BridgeConnectorEA.mq5` in MetaEditor (F7)
2. Attach to any chart (e.g., EURUSD M1)
3. Ensure `EnableTCPBridge = true`, `TCPBridgeHost = 127.0.0.1`, `TCPBridgePort = 8025`
4. Verify: `curl -s http://127.0.0.1:8010/health`

## Project Structure

```
TradeBridge/
├── apps/
│   ├── bridge/main.py              # Entry point: TCP bridge + HTTP/MCP (1 process)
│   ├── mcp_server/main.py          # MCP server (:8010) — FastMCP + REST + gateway
│   │   └── tools_*.py              # 14 MCP tool modules (91 tools across 12 categories)
│   ├── bridge_gateway/             # Shared gateway routes mounted into :8010
│   └── tcp_bridge/                 # TCP bridge (:8025) — embedded in entry point
├── ea/BridgeConnectorEA.mq5        # MQL5 EA for MT5
├── src/mt5_mcp/
│   ├── schemas/                    # Pydantic models
│   ├── services/                   # Business logic (regime, momentum, backtesting, etc.)
│   ├── streaming/                  # WebSocket streaming manager + subscriptions
│   ├── mcp_bridge_protocol.py      # Reusable async TCP bridge protocol for domain-split servers
│   ├── policy/                     # Trade policy enforcement
│   └── observability/              # Logging & metrics
├── deploy/
│   ├── Dockerfile                  # Combined & per-service Docker targets
│   ├── docker-compose.full.yml     # Full stack: redis + tradebridge + mt5-terminal
│   ├── docker-compose.yml          # Separate services (legacy)
│   ├── mt5-terminal/
│   │   ├── Dockerfile              # Headless MT5 (WineHQ 11 + Xvfb)
│   │   ├── start.sh                # Runtime setup & EA patching
│   │   └── BridgeConnectorEA.ex5   # Pre-compiled EA binary
│   └── systemd/                    # Systemd service files
├── skills/mt5-trading/             # AI agent skill (SKILL.md + references)
└── tests/                          # Test suite
```

## Docker Build Reference

```bash
# Build targets
docker build --target combined-target     -t tradebridge:latest .    # All-in-one (recommended)
docker build --target mcp-server-target   -t tradebridge-mcp:latest .
docker build -f deploy/mt5-terminal/Dockerfile -t mt5-terminal:latest .

# Configuration via environment variables
export MT5_MCP_PORT=8010          # HTTP/MCP server port
export MT5_TCP_BRIDGE_PORT=8025    # TCP bridge port
export MT5_REDIS_URL=redis://redis:6379/0  # Redis connection
```

## API Reference

The service exposes **91 MCP tools** (+ REST equivalents, WebSocket streaming) across categories:

| Category | Tools | Requires MT5 | Description |
|----------|-------|-------------|-------------|
| Market Data | 6 | Yes | Bars, indicators, ticks, order book, screenshot, snapshot |
| Trading | 12 | Yes | Market/pending orders, position close/cancel, modify, SL/TP, position sizing, setup validation |
| Analysis | 16 | Yes | Regime, structure, VWAP, divergence, patterns, volume profile, momentum, S/R, volatility, opportunity rank, setup probability, strategy selector, multi-TF indicators, correlation, market scan |
| Resources | 6 | Yes | Account summary, symbol info, deals, performance, positions, orders |
| Portfolio | 4 | Yes | Risk assessment, pre-trade gate, reconciliation, custom indicator |
| Fundamentals | 3 | Partial | Economic risk assessment, market screener, news sentiment |
| Management | 5 | No | Tool status, freeze/thaw, emergency liquidate, safe shutdown |
| Streaming | 3 | No | WebSocket subscribe/unsubscribe/status |
| Metacognition/Context | 5 | No | Trade decision log, reflections, insights, agent/system context |
| Vibe-Trading | 6 | Partial | MCP-based vibe trading interface |
| EA-Native | 12 | Yes | Bracket orders, trailing stops via EA bridge |
| Data | 7 | No | Local data store import/query/stats + MT5 auto-sync |
| Wait | 1 | No | Time-based wait/poll |
| Backtesting | 2 | Partial | HTTP POST /tools/backtest/run + GET /tools/backtest/result/{id} |

**WebSocket:** Subscribe to real-time price/bar updates at `/ws/prices?token=...`

**For AI agents:** The MCP protocol endpoint at `/mcp` (SSE) is the primary interface, with 91 tools auto-discovered. Register with Hermes or any MCP client for automatic tool discovery.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MT5_GATEWAY_URL` | `http://127.0.0.1:8010` | Gateway endpoint (same process, port 8010) |
| `MT5_REDIS_URL` | - | Redis (optional, falls back to in-memory) |
| `MT5_MCP_HOST` | `0.0.0.0` | HTTP/MCP server bind address (:8010) |
| `MT5_TCP_BRIDGE_HOST` | `0.0.0.0` | TCP bridge bind address (:8025) |
| `MT5_TCP_BRIDGE_PORT` | `8025` | TCP bridge port |

## Monitoring

```bash
# Health checks
curl http://localhost:8010/health                          # Full service health
curl http://localhost:8010/tcp-bridge/status                # TCP bridge status

# WebSocket streaming (connect and subscribe)
# ws-client ws://localhost:8010/ws/prices?token=your_token
# {"action": "subscribe", "symbols": ["EURUSD"], "timeframes": ["M1", "H1"]}

# Docker container stats
docker stats --no-stream mt5-combined mt5-terminal mt5-redis
```

## Testing

```bash
# Python tests
poetry run pytest

# Docker end-to-end
docker compose -f deploy/docker-compose.full.yml up -d
curl http://localhost:8010/health
```

## Security

- HTTP/MCP service (:8010) binds to `127.0.0.1` on systemd, `0.0.0.0` in Docker
- MT5 terminal uses `seccomp:unconfined` (required for Wine socket operations)
- Execution tools gated for demo accounts in R&D mode
- Optional `MT5_COMMAND_SECRET` for command authorization
- Policy layer enforces account-level execution policies

## Related Projects

- **[Jesse](https://github.com/ishanp321/jesse)** — Autonomous AI Trading Agent (LangChain ReAct) consuming TradeBridge as trading backend

## License

MIT
