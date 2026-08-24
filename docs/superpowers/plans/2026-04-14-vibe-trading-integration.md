# Vibe-Trading × MetaTraderBridge Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Vibe-Trading's research/strategy/backtesting capabilities with MetaTraderBridge's MT5 execution layer via an external adapter inside MetaTraderBridge/apps/, while maintaining a clean fork workflow that pulls upstream Vibe-Trading updates without merge conflicts.

**Architecture:** The adapter spawns Vibe-Trading's existing MCP server as a managed subprocess and exposes its 17 tools through MetaTraderBridge's bridge gateway. A signal translator converts Vibe-Trading strategy output into MetaTraderBridge-compatible order parameters. Zero modifications to Vibe-Trading core code — it's consumed as a black-box MCP service.

**Tech Stack:** Python 3.11+, FastAPI, MCP protocol (stdio), subprocess management, Pydantic, Poetry, Git (fork + upstream remote)

---

## Git Strategy: Fork + Upstream Remote Workflow

```
HKUDS/Vibe-Trading (upstream, GitHub)
         │
         │ git fetch upstream
         │ git rebase upstream/main
         ▼
ishanp/Vibe-Trading (fork, your GitLab)
         │
         │ your integration branch diverges here
         │
    ┌────┴────────────────────┐
    │ integration/adapter     │  ← your commits (adapter code in MetaTraderBridge/apps/)
    │                         │     + minimal Vibe-Trading tweaks if needed
    │  main                   │  ← clean mirror of upstream (auto-updated)
    └─────────────────────────┘
```

**Why this works:**
- Your fork on GitLab tracks `upstream/main` (HKUDS/Vibe-Trading)
- Your `main` branch stays a clean mirror — you `git fetch upstream && git rebase upstream/main` regularly
- Integration work lives **in MetaTraderBridge/apps/vibe_bridge/** — a separate repo entirely
- If you need to modify Vibe-Trading code, you do it on `integration/adapter` branch and rebase onto `main` after upstream updates
- **Best case:** Zero Vibe-Trading modifications. The adapter connects via MCP protocol only.

**Commands for keeping up-to-date:**
```bash
# One-time setup
cd ~/Documents/GitHub/Vibe-Trading
git remote add upstream https://github.com/HKUDS/Vibe-Trading.git

# Regular update (weekly or when you want new features)
git fetch upstream
git checkout main
git rebase upstream/main
git push origin main

# If you have an integration branch with your modifications:
git checkout integration/adapter
git rebase main  # replays your changes on top of latest upstream
# Resolve conflicts only in files you actually modified
git push origin integration/adapter --force-with-lease
```

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `apps/vibe_bridge/__init__.py` | Create | Package init |
| `apps/vibe_bridge/config.py` | Create | Vibe-Trading connection config, env vars |
| `apps/vibe_bridge/client.py` | Create | MCP subprocess manager + tool invoker |
| `apps/vibe_bridge/signal_translator.py` | Create | Vibe-Trading strategy → MetaTraderBridge orders |
| `apps/vibe_bridge/lifecycle.py` | Create | Process start/stop/health management |
| `apps/vibe_bridge/gateway_routes.py` | Create | FastAPI routes exposed through bridge gateway |
| `apps/vibe_bridge/tools.py` | Create | MetaTraderBridge MCP tool wrappers for Vibe-Trading tools |
| `tests/test_vibe_bridge_client.py` | Create | Tests for MCP subprocess client |
| `tests/test_signal_translator.py` | Create | Tests for signal → order translation |
| `pyproject.toml` | Modify | Add Vibe-Trading as editable dependency |
| `.env.example` | Modify | Add VIBE_TRADING_DIR env var |
| `apps/mcp_server/tools_analysis.py` | Modify | Add Vibe-Trading analysis tools |

---

### Task 1: Fork Setup & Vibe-Trading Dependency

**Files:**
- Modify: (git operations only, no file changes)
- Modify: `pyproject.toml:9-10`
- Create: `.env.example` (add VIBE_TRADING lines)
- Create: `apps/vibe_bridge/__init__.py`

- [ ] **Step 1.1: Set up Git remote for upstream tracking**

Run in Vibe-Trading directory:
```bash
cd ~/Documents/GitHub/Vibe-Trading
# Check current remotes
git remote -v
# Add upstream if not already present
git remote add upstream https://github.com/HKUDS/Vibe-Trading.git
# Verify
git remote -v
# Expected: origin → your GitLab, upstream → HKUDS GitHub
```

- [ ] **Step 1.2: Verify clean main branch**

```bash
cd ~/Documents/GitHub/Vibe-Trading
git checkout main
git status
# Expected: "Your branch is up to date with 'origin/main'"
# If not clean: git stash or commit any local changes to integration branch first
```

- [ ] **Step 1.3: Add Vibe-Trading as editable dependency in MetaTraderBridge**

```toml
# In pyproject.toml, add after line 22 (mcp = "^1.0.0"):
```

Add to `[tool.poetry.dependencies]`:
```toml
vibe-trading = { path = "../Vibe-Trading", develop = true, extras = ["dev"] }
```

Full updated `[tool.poetry.dependencies]` section:
```toml
[tool.poetry.dependencies]
python = ">=3.11,<3.15"
fastapi = "^0.110.0"
uvicorn = {version = "^0.27.0", extras = ["standard"]}
pydantic = "^2.6.0"
typing-extensions = "^4.9.0"
redis = "^5.0.0"
psycopg-binary = "^3.1.18"
structlog = "^24.1.0"
httpx = "^0.27.0"
MetaTrader5 = {version = "^5.0.45", optional = true}
prometheus-client = "^0.20.0"
uvloop = {version = "^0.19.0", markers = "sys_platform != 'win32'"}
mcp = "^1.0.0"
vibe-trading = { path = "../Vibe-Trading", develop = true }
```

- [ ] **Step 1.4: Add env var to .env.example**

Read current `.env.example`, then add:
```bash
# Vibe-Trading Integration (optional — for research/strategy/backtesting)
VIBE_TRADING_DIR=../Vibe-Trading/agent
VIBE_TRADING_MCP_PORT=8900
VIBE_TRADING_LLM_PROVIDER=ollama
VIBE_TRADING_LLM_BASE_URL=http://localhost:11434
VIBE_TRADING_LLM_MODEL=deepseek-r1:latest
```

- [ ] **Step 1.5: Create package init**

```python
# apps/vibe_bridge/__init__.py
"""Vibe-Trading integration adapter.

Spawns Vibe-Trading's MCP server as a managed subprocess and exposes
its research/strategy/backtesting tools through the MetaTraderBridge gateway.
"""

__version__ = "0.1.0"
```

- [ ] **Step 1.6: Install dependencies**

```bash
cd ~/Documents/GitHub/MetaTraderBridge
poetry install
# Expected: resolves Vibe-Trading + all its deps (langchain, fastmcp, akshare, etc.)
# If conflicts: check for version clashes between MetaTraderBridge and Vibe-Trading deps
```

- [ ] **Step 1.7: Commit**

```bash
git add pyproject.toml .env.example apps/vibe_bridge/__init__.py poetry.lock
git commit -m "feat: add Vibe-Trading as editable dependency for integration"
```

---

### Task 2: Vibe-Trading Subprocess Manager (Lifecycle)

**Files:**
- Create: `apps/vibe_bridge/lifecycle.py`
- Create: `apps/vibe_bridge/config.py`
- Test: `tests/test_vibe_lifecycle.py`

- [ ] **Step 2.1: Create config module**

```python
# apps/vibe_bridge/config.py
"""Configuration for Vibe-Trading integration."""

import os
from pathlib import Path
from typing import Optional


def get_vibe_trading_dir() -> Path:
    """Get path to Vibe-Trading agent directory."""
    env_path = os.getenv("VIBE_TRADING_DIR")
    if env_path:
        return Path(env_path).resolve()
    # Default: sibling directory to MetaTraderBridge
    mtbridge_root = Path(__file__).resolve().parent.parent.parent
    default = (mtbridge_root.parent / "Vibe-Trading" / "agent").resolve()
    if default.exists():
        return default
    raise FileNotFoundError(
        f"Vibe-Trading agent directory not found. "
        f"Set VIBE_TRADING_DIR env var or place Vibe-Trading at: {default}"
    )


def get_vibe_mcp_port() -> int:
    """Get Vibe-Trading MCP SSE port."""
    return int(os.getenv("VIBE_TRADING_MCP_PORT", "8900"))


def get_vibe_env_overrides() -> dict[str, str]:
    """Get environment variable overrides for Vibe-Trading subprocess."""
    env = {}
    if provider := os.getenv("VIBE_TRADING_LLM_PROVIDER"):
        env["LANGCHAIN_PROVIDER"] = provider
    if base_url := os.getenv("VIBE_TRADING_LLM_BASE_URL"):
        env[f"{provider.upper()}_BASE_URL"] = base_url
    if model := os.getenv("VIBE_TRADING_LLM_MODEL"):
        env["LANGCHAIN_MODEL_NAME"] = model
    if tushare := os.getenv("TUSHARE_TOKEN"):
        env["TUSHARE_TOKEN"] = tushare
    if timeout := os.getenv("VIBE_TRADING_TIMEOUT"):
        env["TIMEOUT_SECONDS"] = timeout
    return env
```

- [ ] **Step 2.2: Create lifecycle manager**

```python
# apps/vibe_bridge/lifecycle.py
"""Manage Vibe-Trading MCP server subprocess lifecycle."""

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx
import structlog

from .config import get_vibe_env_overrides, get_vibe_mcp_port, get_vibe_trading_dir

logger = structlog.get_logger(__name__)


class VibeBridgeLifecycle:
    """Manages the Vibe-Trading MCP server process."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._port = get_vibe_mcp_port()
        self._vibe_dir = get_vibe_trading_dir()
        self._started = False

    @property
    def is_running(self) -> bool:
        """Check if Vibe-Trading MCP server is running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    @property
    def sse_url(self) -> str:
        """Get the SSE endpoint URL for MCP communication."""
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> bool:
        """Start Vibe-Trading MCP server with SSE transport.

        Returns True if server is ready, False if it failed to start.
        """
        if self.is_running:
            logger.info("vibe-trading-mcp already running")
            return True

        mcp_server_path = self._vibe_dir / "mcp_server.py"
        if not mcp_server_path.exists():
            logger.error("Vibe-Trading mcp_server.py not found", path=str(mcp_server_path))
            return False

        env = os.environ.copy()
        env.update(get_vibe_env_overrides())
        # Ensure agent/ is discoverable
        env["PYTHONPATH"] = str(self._vibe_dir) + os.pathsep + env.get("PYTHONPATH", "")

        try:
            self._process = subprocess.Popen(
                [
                    "python", str(mcp_server_path),
                    "--transport", "sse",
                    "--port", str(self._port),
                ],
                env=env,
                cwd=str(self._vibe_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            logger.info("Started Vibe-Trading MCP server", pid=self._process.pid, port=self._port)
        except Exception as e:
            logger.error("Failed to start Vibe-Trading MCP server", error=str(e))
            return False

        # Wait for server to be ready (up to 15 seconds)
        for attempt in range(30):
            await asyncio.sleep(0.5)
            if await self._check_health():
                self._started = True
                logger.info("Vibe-Trading MCP server ready", url=self.sse_url)
                return True

        logger.error("Vibe-Trading MCP server failed to start within 15s timeout")
        await self.stop()
        return False

    async def stop(self) -> None:
        """Stop the Vibe-Trading MCP server gracefully."""
        if self._process is None:
            return

        if self._process.poll() is None:
            logger.info("Stopping Vibe-Trading MCP server", pid=self._process.pid)
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                self._process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                self._process.wait(timeout=3)

        self._process = None
        self._started = False
        logger.info("Vibe-Trading MCP server stopped")

    async def _check_health(self) -> bool:
        """Check if the SSE endpoint is responding."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                # SSE endpoint responds with 200 + text/event-stream
                resp = await client.get(f"{self.sse_url}/sse")
                return resp.status_code == 200
        except Exception:
            return False

    async def ensure_running(self) -> bool:
        """Start if not running, return health status."""
        if self.is_running:
            return await self._check_health()
        return await self.start()

    def get_status(self) -> dict:
        """Get current status of Vibe-Trading subprocess."""
        return {
            "running": self.is_running,
            "started": self._started,
            "pid": self._process.pid if self._process else None,
            "port": self._port,
            "sse_url": self.sse_url if self.is_running else None,
            "exit_code": self._process.poll() if self._process else None,
        }
```

- [ ] **Step 2.3: Write lifecycle tests**

```python
# tests/test_vibe_lifecycle.py
"""Tests for Vibe-Trading lifecycle management."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.vibe_bridge.lifecycle import VibeBridgeLifecycle


@pytest.fixture
def lifecycle():
    """Create lifecycle with mocked subprocess."""
    with patch("apps.vibe_bridge.lifecycle.get_vibe_trading_dir") as mock_dir:
        mock_dir.return_value = MagicMock()
        mock_dir.return_value.__truediv__.return_value.exists.return_value = True
        with patch("apps.vibe_bridge.lifecycle.get_vibe_mcp_port", return_value=18900):
            with patch("apps.vibe_bridge.lifecycle.get_vibe_env_overrides", return_value={}):
                yield VibeBridgeLifecycle()


class TestVibeBridgeLifecycle:
    def test_is_running_false_initially(self, lifecycle):
        assert lifecycle.is_running is False

    def test_get_status_initial(self, lifecycle):
        status = lifecycle.get_status()
        assert status["running"] is False
        assert status["pid"] is None
        assert status["port"] == 18900

    @pytest.mark.asyncio
    async def test_start_process_not_found(self, lifecycle):
        """Should return False when mcp_server.py doesn't exist."""
        lifecycle._vibe_dir.__truediv__.return_value.exists.return_value = False
        result = await lifecycle.start()
        assert result is False

    @pytest.mark.asyncio
    async def test_ensure_running_starts_if_not_running(self, lifecycle):
        """Should start the process when not running."""
        with patch.object(lifecycle, "start", new_callable=AsyncMock) as mock_start:
            mock_start.return_value = True
            result = await lifecycle.ensure_running()
            mock_start.assert_called_once()
            assert result is True
```

- [ ] **Step 2.4: Run tests**

```bash
cd ~/Documents/GitHub/MetaTraderBridge
poetry run pytest tests/test_vibe_lifecycle.py -v
# Expected: 4 tests pass
```

- [ ] **Step 2.5: Commit**

```bash
git add apps/vibe_bridge/lifecycle.py apps/vibe_bridge/config.py tests/test_vibe_lifecycle.py
git commit -m "feat: add Vibe-Trading subprocess lifecycle manager with health checks"
```

---

### Task 3: MCP Client — Tool Invocation Over SSE

**Files:**
- Create: `apps/vibe_bridge/client.py`
- Test: `tests/test_vibe_bridge_client.py`

- [ ] **Step 3.1: Create MCP client**

```python
# apps/vibe_bridge/client.py
"""MCP client for communicating with Vibe-Trading over SSE."""

import json
from typing import Any, Optional

import httpx
import structlog

from .config import get_vibe_mcp_port
from .lifecycle import VibeBridgeLifecycle

logger = structlog.get_logger(__name__)

# All 17 Vibe-Trading MCP tools
VIBE_TOOLS = [
    "list_skills",
    "load_skill",
    "backtest",
    "factor_analysis",
    "analyze_options",
    "pattern_recognition",
    "get_market_data",
    "web_search",
    "read_url",
    "read_document",
    "read_file",
    "write_file",
    "list_swarm_presets",
    "run_swarm",
    "get_swarm_status",
    "get_run_result",
    "list_runs",
]


class VibeBridgeClient:
    """HTTP client for Vibe-Trading MCP tools over SSE transport."""

    def __init__(self, lifecycle: Optional[VibeBridgeLifecycle] = None) -> None:
        self._lifecycle = lifecycle or VibeBridgeLifecycle()
        self._port = get_vibe_mcp_port()
        self._session_id: Optional[str] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def ensure_ready(self) -> bool:
        """Ensure Vibe-Trading MCP server is running and ready."""
        return await self._lifecycle.ensure_running()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call a Vibe-Trading MCP tool by name.

        Args:
            tool_name: One of the 17 Vibe-Trading tool names.
            arguments: Tool-specific arguments dict.

        Returns:
            Raw string response from the tool (usually JSON).

        Raises:
            RuntimeError: If server is not running.
            ValueError: If tool_name is not a valid Vibe-Trading tool.
        """
        if tool_name not in VIBE_TOOLS:
            raise ValueError(
                f"Unknown Vibe-Trading tool: {tool_name}. "
                f"Valid tools: {VIBE_TOOLS}"
            )

        if not self._lifecycle.is_running:
            raise RuntimeError("Vibe-Trading MCP server is not running. Call ensure_ready() first.")

        # Vibe-Trading MCP over SSE exposes tools via POST /messages
        # with JSON-RPC style payloads. We use the FastAPI server directly
        # via its REST API instead, which is simpler and more reliable.
        # The FastAPI server is on the same port.
        return await self._call_rest_api(tool_name, arguments or {})

    async def _call_rest_api(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call Vibe-Trading via its FastAPI REST API (more reliable than SSE for tools)."""
        # Map tool names to FastAPI endpoints or invoke via MCP JSON-RPC
        # Vibe-Trading's SSE MCP follows the MCP spec: POST /messages with JSON-RPC
        async with httpx.AsyncClient(timeout=120.0) as client:
            # MCP JSON-RPC request over SSE
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }
            try:
                resp = await client.post(
                    f"{self.base_url}/messages",
                    json=request,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 202:
                    # SSE accepts request, need to read from stream
                    return await self._read_sse_response(client, resp)
                else:
                    return json.dumps({
                        "status": "error",
                        "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
                    })
            except httpx.ConnectError:
                return json.dumps({
                    "status": "error",
                    "error": f"Cannot connect to Vibe-Trading at {self.base_url}",
                })
            except httpx.TimeoutException:
                return json.dumps({
                    "status": "error",
                    "error": f"Vibe-Trading tool '{tool_name}' timed out after 120s",
                })

    async def _read_sse_response(self, client: httpx.AsyncClient, accept_resp) -> str:
        """Read the response from SSE stream after a tool call."""
        # SSE response: read events until we get the result
        result_line = ""
        async for line in accept_resp.aiter_lines():
            if line.startswith("data: "):
                data = line[6:]  # strip "data: "
                try:
                    parsed = json.loads(data)
                    if parsed.get("result") is not None:
                        content = parsed["result"].get("content", [])
                        if content and isinstance(content, list):
                            # MCP tool result: content is array of {type, text}
                            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                            return "\n".join(texts)
                        return json.dumps(parsed["result"])
                    if parsed.get("error"):
                        return json.dumps({"status": "error", "error": str(parsed["error"])})
                except json.JSONDecodeError:
                    result_line = data
        return result_line or json.dumps({"status": "error", "error": "No response from SSE stream"})

    async def list_skills(self) -> str:
        """List all 69 Vibe-Trading finance skills."""
        return await self.call_tool("list_skills")

    async def get_market_data(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        source: str = "auto",
        interval: str = "1D",
    ) -> str:
        """Fetch OHLCV market data."""
        return await self.call_tool("get_market_data", {
            "codes": codes,
            "start_date": start_date,
            "end_date": end_date,
            "source": source,
            "interval": interval,
        })

    async def run_swarm(self, preset_name: str, variables: dict[str, str]) -> str:
        """Run a multi-agent swarm team."""
        return await self.call_tool("run_swarm", {
            "preset_name": preset_name,
            "variables": variables,
        })

    async def backtest(self, run_dir: str) -> str:
        """Run a backtest."""
        return await self.call_tool("backtest", {"run_dir": run_dir})

    async def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web."""
        return await self.call_tool("web_search", {
            "query": query,
            "max_results": max_results,
        })

    def get_status(self) -> dict:
        """Get combined status of lifecycle and client."""
        return self._lifecycle.get_status()
```

- [ ] **Step 3.2: Write client tests**

```python
# tests/test_vibe_bridge_client.py
"""Tests for Vibe-Trading MCP client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.vibe_bridge.client import VibeBridgeClient, VIBE_TOOLS


@pytest.fixture
def client():
    """Create client with mocked lifecycle."""
    mock_lifecycle = MagicMock()
    mock_lifecycle.is_running = True
    mock_lifecycle.ensure_running = AsyncMock(return_value=True)
    with patch("apps.vibe_bridge.client.get_vibe_mcp_port", return_value=18900):
        yield VibeBridgeClient(lifecycle=mock_lifecycle)


class TestVibeBridgeClient:
    def test_valid_tools_list(self):
        assert len(VIBE_TOOLS) == 17
        assert "backtest" in VIBE_TOOLS
        assert "run_swarm" in VIBE_TOOLS
        assert "get_market_data" in VIBE_TOOLS

    def test_call_invalid_tool_raises(self, client):
        import asyncio
        with pytest.raises(ValueError, match="Unknown Vibe-Trading tool"):
            asyncio.get_event_loop().run_until_complete(
                client.call_tool("nonexistent_tool")
            )

    @pytest.mark.asyncio
    async def test_call_tool_when_not_running_raises(self):
        mock_lifecycle = MagicMock()
        mock_lifecycle.is_running = False
        client = VibeBridgeClient(lifecycle=mock_lifecycle)
        with pytest.raises(RuntimeError, match="not running"):
            await client.call_tool("list_skills")

    @pytest.mark.asyncio
    async def test_ensure_ready_calls_lifecycle(self, client):
        result = await client.ensure_ready()
        client._lifecycle.ensure_running.assert_called_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_status(self, client):
        client._lifecycle.get_status.return_value = {"running": True, "pid": 12345}
        status = client.get_status()
        assert status["running"] is True
        assert status["pid"] == 12345
```

- [ ] **Step 3.3: Run tests**

```bash
poetry run pytest tests/test_vibe_bridge_client.py tests/test_vibe_lifecycle.py -v
# Expected: all tests pass
```

- [ ] **Step 3.4: Commit**

```bash
git add apps/vibe_bridge/client.py tests/test_vibe_bridge_client.py
git commit -m "feat: add Vibe-Trading MCP client with tool invocation over SSE"
```

---

### Task 4: Signal Translator — Strategy Output → MT5 Orders

**Files:**
- Create: `apps/vibe_bridge/signal_translator.py`
- Test: `tests/test_signal_translator.py`

- [ ] **Step 4.1: Create signal translator**

```python
# apps/vibe_bridge/signal_translator.py
"""Translate Vibe-Trading strategy/research output into MetaTraderBridge-compatible orders.

Vibe-Trading produces research reports, swarm debates, and backtest results.
This module extracts actionable signals and maps them to MetaTraderBridge order schemas.
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


class SignalStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


@dataclass
class TradeSignal:
    """A tradeable signal extracted from Vibe-Trading output."""
    action: SignalAction
    symbol: str  # e.g. "XAUUSD", "EURUSD"
    strength: SignalStrength = SignalStrength.MODERATE
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 0.5  # 0.0 to 1.0
    reasoning: str = ""
    source: str = ""  # e.g. "swarm:investment_committee", "backtest:abc123"
    timeframe: str = "H1"  # MT5 timeframe recommendation
    risk_reward: Optional[float] = None

    def to_order_params(self) -> dict:
        """Convert signal to MetaTraderBridge submit_order parameters."""
        params = {
            "symbol": self._map_symbol(self.symbol),
            "action": self.action.value,
        }
        if self.entry_price is not None:
            params["price"] = self.entry_price
        if self.stop_loss is not None:
            params["sl"] = self.stop_loss
        if self.take_profit is not None:
            params["tp"] = self.take_profit
        if self.timeframe:
            params["timeframe"] = self.timeframe
        if self.reasoning:
            params["comment"] = f"Vibe: {self.reasoning[:100]}"
        return params

    @staticmethod
    def _map_symbol(symbol: str) -> str:
        """Map Vibe-Trading symbol format to MT5 symbol format."""
        mapping = {
            "XAU/USD": "XAUUSD",
            "XAUUSDm": "XAUUSD",
            "GOLD": "XAUUSD",
            "EUR/USD": "EURUSD",
            "GBP/USD": "GBPUSD",
            "USD/JPY": "USDJPY",
            "BTC-USDT": "BTCUSD",  # MT5 may not have BTC, depends on broker
            "ETH-USDT": "ETHUSD",
        }
        return mapping.get(symbol.upper(), symbol.upper().replace("/", "").replace("-", ""))


def extract_signal_from_swarm_report(report: str, preset: str = "") -> Optional[TradeSignal]:
    """Extract trade signal from a swarm team final report.

    Parses the report for actionable trade recommendations with
    entry/exit levels.
    """
    # Look for common patterns in swarm reports
    # Pattern 1: "BUY/SELL XAUUSD at ..."
    action_match = re.search(r"\b(BUY|SELL|CLOSE)\b\s+([A-Z]{3,8}[/\-]?[A-Z]{3})", report, re.I)
    if not action_match:
        return None

    action = SignalAction(action_match.group(1).upper())
    raw_symbol = action_match.group(2)

    # Extract price levels
    entry = _extract_price(report, r"(?:entry|enter|open)\s*(?:at|price)?:?\s*\$?([\d,]+\.?\d*)")
    sl = _extract_price(report, r"(?:stop[\s-]?loss|SL|stop)\s*(?:at|:)?\s*\$?([\d,]+\.?\d*)")
    tp = _extract_price(report, r"(?:take[\s-]?profit|TP|target)\s*(?:at|:)?\s*\$?([\d,]+\.?\d*)")

    # Extract confidence
    confidence = _extract_confidence(report)

    # Extract reasoning (first paragraph after the signal)
    reasoning = _extract_reasoning(report)

    # Calculate risk/reward
    risk_reward = None
    if entry and sl and tp and entry != sl:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk > 0:
            risk_reward = round(reward / risk, 2)

    strength = SignalStrength.STRONG if confidence >= 0.75 else (
        SignalStrength.MODERATE if confidence >= 0.5 else SignalStrength.WEAK
    )

    return TradeSignal(
        action=action,
        symbol=raw_symbol,
        strength=strength,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        confidence=confidence,
        reasoning=reasoning,
        source=f"swarm:{preset}" if preset else "swarm",
        risk_reward=risk_reward,
    )


def extract_signal_from_backtest(result: str, symbol: str = "") -> Optional[TradeSignal]:
    """Extract trade viability from backtest results.

    If backtest shows positive Sharpe + acceptable drawdown,
    generate a HOLD/BUY signal to proceed with live execution.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None

    sharpe = _safe_float(data.get("sharpe_ratio") or data.get("sharpe"))
    max_dd = _safe_float(data.get("max_drawdown") or data.get("max_dd"))
    win_rate = _safe_float(data.get("win_rate") or data.get("win_rate_pct"))
    total_return = _safe_float(data.get("total_return") or data.get("return_pct"))

    # Decision logic: is this strategy worth executing live?
    if sharpe is None and total_return is None:
        return None

    # Good backtest → signal to proceed
    if (sharpe is not None and sharpe > 1.0) or (total_return is not None and total_return > 0):
        confidence = min(0.9, max(0.3, (sharpe or 0) / 3.0 + 0.3))
        action = SignalAction.BUY if (total_return or 0) > 0 else SignalAction.SELL
        reasoning = f"Backtest: Sharpe={sharpe}, MaxDD={max_dd}%, WR={win_rate}%, Return={total_return}%"
        return TradeSignal(
            action=action,
            symbol=symbol or "UNKNOWN",
            strength=SignalStrength.MODERATE if sharpe and sharpe > 1.0 else SignalStrength.WEAK,
            confidence=confidence,
            reasoning=reasoning,
            source="backtest",
        )

    return TradeSignal(
        action=SignalAction.HOLD,
        symbol=symbol or "UNKNOWN",
        confidence=0.2,
        reasoning=f"Backtest not compelling: Sharpe={sharpe}, MaxDD={max_dd}%",
        source="backtest",
    )


def _extract_price(text: str, pattern: str) -> Optional[float]:
    """Extract a price value from text using regex pattern."""
    match = re.search(pattern, text, re.I)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except (ValueError, IndexError):
            return None
    return None


def _extract_confidence(text: str) -> float:
    """Extract confidence level from text."""
    # Look for percentage
    match = re.search(r"confidence[:\s]+(\d+)%", text, re.I)
    if match:
        return int(match.group(1)) / 100.0
    # Look for decimal
    match = re.search(r"confidence[:\s]+(0\.\d+)", text, re.I)
    if match:
        return float(match.group(1))
    # Default moderate
    return 0.5


def _extract_reasoning(text: str) -> str:
    """Extract first 200 chars of reasoning from text."""
    # Clean up and truncate
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:200]


def _safe_float(value) -> Optional[float]:
    """Safely convert value to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 4.2: Write signal translator tests**

```python
# tests/test_signal_translator.py
"""Tests for Vibe-Trading signal translator."""

import json

from apps.vibe_bridge.signal_translator import (
    SignalAction,
    SignalStrength,
    TradeSignal,
    extract_signal_from_backtest,
    extract_signal_from_swarm_report,
)


class TestTradeSignal:
    def test_symbol_mapping_xau(self):
        signal = TradeSignal(action=SignalAction.BUY, symbol="XAU/USD")
        params = signal.to_order_params()
        assert params["symbol"] == "XAUUSD"

    def test_symbol_mapping_btc(self):
        signal = TradeSignal(action=SignalAction.BUY, symbol="BTC-USDT")
        params = signal.to_order_params()
        assert params["symbol"] == "BTCUSD"

    def test_symbol_mapping_already_mt5(self):
        signal = TradeSignal(action=SignalAction.SELL, symbol="EURUSD")
        params = signal.to_order_params()
        assert params["symbol"] == "EURUSD"

    def test_to_order_params_includes_levels(self):
        signal = TradeSignal(
            action=SignalAction.BUY,
            symbol="XAUUSD",
            entry_price=2350.0,
            stop_loss=2340.0,
            take_profit=2380.0,
        )
        params = signal.to_order_params()
        assert params["price"] == 2350.0
        assert params["sl"] == 2340.0
        assert params["tp"] == 2380.0

    def test_risk_reward_calculation(self):
        signal = TradeSignal(
            action=SignalAction.BUY,
            symbol="EURUSD",
            entry_price=1.0850,
            stop_loss=1.0830,
            take_profit=1.0910,
        )
        assert signal.risk_reward == 3.0  # 60 pip reward / 20 pip risk


class TestExtractSignalFromSwarm:
    def test_buy_signal_extraction(self):
        report = """
        Investment Committee Decision:
        BUY XAUUSD at $2350.00
        Entry at 2350.00, stop loss at 2340.00
        Take profit at 2380.00
        Confidence: 75%
        The gold market shows strong bullish momentum due to geopolitical uncertainty.
        """
        signal = extract_signal_from_swarm_report(report, "investment_committee")
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.entry_price == 2350.0
        assert signal.stop_loss == 2340.0
        assert signal.take_profit == 2380.0
        assert signal.confidence == 0.75
        assert signal.strength == SignalStrength.STRONG
        assert "investment_committee" in signal.source

    def test_sell_signal_extraction(self):
        report = "Recommendation: SELL EUR/USD. Target: 1.0800. SL: 1.0900."
        signal = extract_signal_from_swarm_report(report)
        assert signal is not None
        assert signal.action == SignalAction.SELL
        assert "EURUSD" in signal.symbol  # mapped from EUR/USD

    def test_no_signal_in_report(self):
        report = "The market is consolidating. No clear direction at this time."
        signal = extract_signal_from_swarm_report(report)
        assert signal is None


class TestExtractSignalFromBacktest:
    def test_good_backtest_generates_buy(self):
        result = json.dumps({
            "sharpe_ratio": 1.5,
            "max_drawdown": 0.12,
            "win_rate": 0.62,
            "total_return": 0.25,
        })
        signal = extract_signal_from_backtest(result, "XAUUSD")
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.confidence > 0.3
        assert "Backtest" in signal.reasoning

    def test_bad_backtest_generates_hold(self):
        result = json.dumps({
            "sharpe_ratio": -0.3,
            "max_drawdown": 0.45,
            "total_return": -0.15,
        })
        signal = extract_signal_from_backtest(result, "EURUSD")
        assert signal is not None
        assert signal.action == SignalAction.HOLD

    def test_invalid_json_returns_none(self):
        assert extract_signal_from_backtest("not json") is None
```

- [ ] **Step 4.3: Run tests**

```bash
poetry run pytest tests/test_signal_translator.py -v
# Expected: 11 tests pass
```

- [ ] **Step 4.4: Commit**

```bash
git add apps/vibe_bridge/signal_translator.py tests/test_signal_translator.py
git commit -m "feat: add signal translator for Vibe-Trading → MetaTraderBridge order mapping"
```

---

### Task 5: Gateway Routes — Expose Vibe-Trading Through MetaTraderBridge

**Files:**
- Create: `apps/vibe_bridge/gateway_routes.py`
- Modify: `apps/bridge_gateway/main.py`
- Test: `tests/test_vibe_gateway_routes.py`

- [ ] **Step 5.1: Create gateway routes**

```python
# apps/vibe_bridge/gateway_routes.py
"""FastAPI routes exposing Vibe-Trading capabilities through MetaTraderBridge gateway."""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .client import VibeBridgeClient
from .lifecycle import VibeBridgeLifecycle
from .signal_translator import (
    extract_signal_from_backtest,
    extract_signal_from_swarm_report,
)

router = APIRouter(prefix="/vibe", tags=["vibe-trading"])

# Singleton client
_client: Optional[VibeBridgeClient] = None


def get_client() -> VibeBridgeClient:
    """Get or create VibeBridgeClient singleton."""
    global _client
    if _client is None:
        _client = VibeBridgeClient()
    return _client


# --- Request/Response models ---

class VibeToolRequest(BaseModel):
    tool: str = Field(..., description="Vibe-Trading tool name")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")


class VibeMarketDataRequest(BaseModel):
    codes: list[str] = Field(..., description="Symbols, e.g. ['BTC-USDT', 'AAPL.US']")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    source: str = Field(default="auto")
    interval: str = Field(default="1D")


class VibeSwarmRunRequest(BaseModel):
    preset: str = Field(..., description="Swarm preset name, e.g. 'investment_committee'")
    variables: dict[str, str] = Field(..., description="Preset variables")


class VibeBacktestRequest(BaseModel):
    run_dir: str = Field(..., description="Path to backtest run directory")
    auto_execute: bool = Field(
        default=False,
        description="If true and backtest is good, generate MetaTraderBridge orders",
    )
    symbol: str = Field(default="", description="Symbol for order mapping")


# --- Routes ---

@router.get("/status")
async def vibe_status():
    """Get Vibe-Trading subprocess status."""
    client = get_client()
    return client.get_status()


@router.post("/start")
async def vibe_start():
    """Start Vibe-Trading MCP server."""
    client = get_client()
    started = await client.ensure_ready()
    if not started:
        raise HTTPException(status_code=503, detail="Failed to start Vibe-Trading MCP server")
    return {"status": "running", "url": client.base_url}


@router.post("/stop")
async def vibe_stop():
    """Stop Vibe-Trading MCP server."""
    client = get_client()
    await client._lifecycle.stop()
    return {"status": "stopped"}


@router.post("/tool")
async def vibe_call_tool(req: VibeToolRequest):
    """Call any Vibe-Trading MCP tool.

    Available tools: list_skills, load_skill, backtest, factor_analysis,
    analyze_options, pattern_recognition, get_market_data, web_search,
    read_url, read_document, read_file, write_file, list_swarm_presets,
    run_swarm, get_swarm_status, get_run_result, list_runs.
    """
    client = get_client()
    await client.ensure_ready()
    result = await client.call_tool(req.tool, req.arguments)
    return {"tool": req.tool, "result": result}


@router.get("/skills")
async def vibe_list_skills():
    """List all 69 Vibe-Trading finance skills."""
    client = get_client()
    await client.ensure_ready()
    result = await client.list_skills()
    return {"skills": result}


@router.post("/market-data")
async def vibe_market_data(req: VibeMarketDataRequest):
    """Fetch market data via Vibe-Trading (multi-source: crypto, equities, futures)."""
    client = get_client()
    await client.ensure_ready()
    result = await client.get_market_data(
        codes=req.codes,
        start_date=req.start_date,
        end_date=req.end_date,
        source=req.source,
        interval=req.interval,
    )
    return {"data": result}


@router.post("/swarm/run")
async def vibe_swarm_run(req: VibeSwarmRunRequest):
    """Run a Vibe-Trading swarm team (long-running, may take minutes)."""
    client = get_client()
    await client.ensure_ready()
    result = await client.run_swarm(req.preset, req.variables)
    return {"result": result}


@router.get("/swarm/presets")
async def vibe_swarm_presets():
    """List available swarm team presets."""
    client = get_client()
    await client.ensure_ready()
    result = await client.call_tool("list_swarm_presets")
    return {"presets": result}


@router.post("/backtest")
async def vibe_backtest(req: VibeBacktestRequest):
    """Run a backtest and optionally generate MetaTraderBridge orders from results."""
    import json

    client = get_client()
    await client.ensure_ready()
    result = await client.backtest(req.run_dir)

    response = {"backtest_result": result}

    if req.auto_execute and req.symbol:
        signal = extract_signal_from_backtest(result, req.symbol)
        if signal and signal.action.value in ("BUY", "SELL"):
            response["signal"] = {
                "action": signal.action.value,
                "symbol": signal.symbol,
                "confidence": signal.confidence,
                "reasoning": signal.reasoning,
                "order_params": signal.to_order_params(),
            }
        else:
            response["signal"] = {
                "action": "HOLD",
                "reason": "Backtest not compelling enough for live execution",
            }

    return response


@router.post("/swarm/analyze-and-trade")
async def vibe_swarm_analyze_and_trade(req: VibeSwarmRunRequest):
    """Run swarm analysis and extract tradeable signals.

    This is the main integration endpoint: runs a swarm team,
    extracts trade signals from the report, and returns
    MetaTraderBridge-compatible order parameters.
    """
    import json

    client = get_client()
    await client.ensure_ready()

    # Step 1: Run swarm
    result_str = await client.run_swarm(req.preset, req.variables)

    try:
        result = json.loads(result_str)
    except json.JSONDecodeError:
        result = {"raw": result_str}

    report = result.get("final_report", result_str)

    # Step 2: Extract signal
    signal = extract_signal_from_swarm_report(report, req.preset)

    if signal:
        return {
            "swarm_run_id": result.get("run_id"),
            "signal": {
                "action": signal.action.value,
                "symbol": signal.symbol,
                "strength": signal.strength.value,
                "confidence": signal.confidence,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "risk_reward": signal.risk_reward,
                "order_params": signal.to_order_params(),
            },
            "report_summary": report[:500] if isinstance(report, str) else "",
        }
    else:
        return {
            "swarm_run_id": result.get("run_id"),
            "signal": None,
            "message": "No actionable trade signal found in swarm report",
            "report_summary": report[:500] if isinstance(report, str) else "",
        }
```

- [ ] **Step 5.2: Register routes in bridge gateway**

Read the current `apps/bridge_gateway/main.py` and add the vibe routes. Find the line where other routers are included and add after it:

```python
# Add after existing router includes, before the app definition or alongside them:
from apps.vibe_bridge.gateway_routes import router as vibe_router

app.include_router(vibe_router, prefix="/api")
```

- [ ] **Step 5.3: Commit**

```bash
git add apps/vibe_bridge/gateway_routes.py apps/bridge_gateway/main.py
git commit -m "feat: expose Vibe-Trading routes through MetaTraderBridge gateway"
```

---

### Task 6: MCP Tool Integration — Add Vibe-Trading Tools to MetaTraderBridge MCP

**Files:**
- Create: `apps/vibe_bridge/tools.py`
- Modify: `apps/mcp_server/tools_analysis.py`

- [ ] **Step 6.1: Create MCP tool wrappers**

```python
# apps/vibe_bridge/tools.py
"""MetaTraderBridge MCP tool wrappers for Vibe-Trading capabilities.

These tools are registered with the MetaTraderBridge MCP server and
forward calls to the Vibe-Trading subprocess via the gateway client.
"""

import json
from typing import Any, Optional

from .client import VibeBridgeClient
from .signal_translator import extract_signal_from_swarm_report

_client: Optional[VibeBridgeClient] = None


def _get_client() -> VibeBridgeClient:
    global _client
    if _client is None:
        _client = VibeBridgeClient()
    return _client


async def vibe_list_skills() -> str:
    """List all Vibe-Trading finance skills (69 skills across 7 categories)."""
    client = _get_client()
    await client.ensure_ready()
    return await client.list_skills()


async def vibe_get_market_data(
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
) -> str:
    """Fetch market data from Vibe-Trading's multi-source loaders.

    Supports A-shares, HK/US equities, crypto, futures, forex.
    Use this to get cross-market context before executing MT5 trades.
    """
    client = _get_client()
    await client.ensure_ready()
    return await client.get_market_data(codes, start_date, end_date, source, interval)


async def vibe_run_swarm(preset: str, variables: dict[str, str]) -> str:
    """Run a Vibe-Trading multi-agent swarm team.

    Presets include: investment_committee, crypto_trading_desk,
    quant_strategy_desk, risk_committee, global_allocation_committee.

    Returns: JSON with run_id, final_report, and task statuses.
    """
    client = _get_client()
    await client.ensure_ready()
    return await client.run_swarm(preset, variables)


async def vibe_backtest(run_dir: str) -> str:
    """Run a backtest via Vibe-Trading.

    The run_dir must contain config.json and code/signal_engine.py.
    Supports 7 market engines + composite cross-market engine.
    """
    client = _get_client()
    await client.ensure_ready()
    return await client.backtest(run_dir)


async def vibe_swarm_to_signal(report: str, preset: str = "") -> str:
    """Extract tradeable signal from a Vibe-Trading swarm report.

    Parses the report for BUY/SELL recommendations with entry/exit levels
    and returns MetaTraderBridge-compatible order parameters.
    """
    signal = extract_signal_from_swarm_report(report, preset)
    if signal is None:
        return json.dumps({
            "status": "no_signal",
            "message": "No actionable trade signal found in report",
        })
    return json.dumps({
        "status": "signal_extracted",
        "action": signal.action.value,
        "symbol": signal.symbol,
        "strength": signal.strength.value,
        "confidence": signal.confidence,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "risk_reward": signal.risk_reward,
        "order_params": signal.to_order_params(),
    })


async def vibe_web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Vibe-Trading for market news and sentiment."""
    client = _get_client()
    await client.ensure_ready()
    return await client.web_search(query, max_results)
```

- [ ] **Step 6.2: Register tools in MetaTraderBridge MCP server**

Read `apps/mcp_server/main.py` and `apps/mcp_server/tools_analysis.py`. Add the vibe tools to the analysis module following the existing pattern. Each tool should follow the same registration pattern as existing tools.

For example, in `tools_analysis.py`, add:
```python
# At the top, add import:
from apps.vibe_bridge import tools as vibe_tools

# Then register each tool following the existing pattern for MCP tool registration
```

The exact registration depends on MetaTraderBridge's existing tool registration pattern. Follow whatever convention is already used (decorator, registration function, etc.).

- [ ] **Step 6.3: Commit**

```bash
git add apps/vibe_bridge/tools.py apps/mcp_server/tools_analysis.py
git commit -m "feat: expose Vibe-Trading tools through MetaTraderBridge MCP server"
```

---

### Task 7: Startup Integration & Health Checks

**Files:**
- Modify: `apps/bridge_gateway/main.py` (startup/shutdown events)
- Modify: `apps/mcp_server/main.py` (health endpoint)

- [ ] **Step 7.1: Add startup/shutdown events to bridge gateway**

In `apps/bridge_gateway/main.py`, add lifespan events:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

# ... existing imports ...

from apps.vibe_bridge.lifecycle import VibeBridgeLifecycle

_vibe_lifecycle: Optional[VibeBridgeLifecycle] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage Vibe-Trading subprocess lifecycle."""
    global _vibe_lifecycle
    _vibe_lifecycle = VibeBridgeLifecycle()

    # Auto-start if configured
    if os.getenv("VIBE_TRADING_AUTO_START", "false").lower() == "true":
        try:
            await _vibe_lifecycle.start()
        except Exception as e:
            logger.warning("Failed to auto-start Vibe-Trading", error=str(e))

    yield

    # Shutdown
    if _vibe_lifecycle:
        await _vibe_lifecycle.stop()
```

- [ ] **Step 7.2: Add to .env.example**

```bash
# Auto-start Vibe-Trading MCP server on gateway startup
VIBE_TRADING_AUTO_START=false
```

- [ ] **Step 7.3: Commit**

```bash
git add apps/bridge_gateway/main.py .env.example
git commit -m "feat: add Vibe-Trading lifecycle management to gateway startup/shutdown"
```

---

### Task 8: End-to-End Integration Test

**Files:**
- Create: `tests/test_vibe_integration.py`

- [ ] **Step 8.1: Create integration test**

```python
# tests/test_vibe_integration.py
"""Integration tests for Vibe-Trading ↔ MetaTraderBridge bridge."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.vibe_bridge.client import VibeBridgeClient
from apps.vibe_bridge.gateway_routes import (
    vibe_backtest,
    vibe_market_data,
    vibe_status,
)
from apps.vibe_bridge.signal_translator import (
    SignalAction,
    extract_signal_from_backtest,
    extract_signal_from_swarm_report,
)
from apps.vibe_bridge.tools import vibe_swarm_to_signal


class TestSignalPipeline:
    """Test the full pipeline: swarm report → signal extraction → order params."""

    def test_full_pipeline_buy_signal(self):
        report = """
        The Investment Committee has reached a decision:
        BUY XAUUSD at $2350.00
        Entry at 2350.00, stop loss at 2340.00
        Take profit at 2380.00
        Confidence: 75%

        Rationale: Strong bullish momentum driven by Fed policy uncertainty.
        """
        signal = extract_signal_from_swarm_report(report, "investment_committee")
        assert signal is not None
        assert signal.action == SignalAction.BUY

        order_params = signal.to_order_params()
        assert order_params["symbol"] == "XAUUSD"
        assert order_params["price"] == 2350.0
        assert order_params["sl"] == 2340.0
        assert order_params["tp"] == 2380.0

    def test_full_pipeline_backtest_to_order(self):
        result = json.dumps({
            "sharpe_ratio": 1.5,
            "max_drawdown": 0.12,
            "win_rate": 0.62,
            "total_return": 0.25,
        })
        signal = extract_signal_from_backtest(result, "XAUUSD")
        assert signal is not None
        assert signal.action == SignalAction.BUY

    def test_swarm_to_signal_tool(self):
        import asyncio
        report = "SELL EUR/USD at 1.0850. Stop loss at 1.0900. Target 1.0750."
        result = asyncio.get_event_loop().run_until_complete(
            vibe_swarm_to_signal(report, "test")
        )
        data = json.loads(result)
        assert data["status"] == "signal_extracted"
        assert data["action"] == "SELL"
        assert "order_params" in data


class TestGatewayRoutes:
    """Test gateway route handlers."""

    @pytest.mark.asyncio
    async def test_vibe_status_when_not_running(self):
        with patch("apps.vibe_bridge.gateway_routes.get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.get_status.return_value = {"running": False, "pid": None}
            mock_get.return_value = mock_client
            result = await vibe_status()
            assert result["running"] is False
```

- [ ] **Step 8.2: Run all Vibe-Trading tests**

```bash
poetry run pytest tests/test_vibe*.py -v --tb=short
# Expected: all tests pass
```

- [ ] **Step 8.3: Commit**

```bash
git add tests/test_vibe_integration.py
git commit -m "test: add end-to-end integration tests for Vibe-Trading bridge"
```

---

## Upstream Update Workflow (Ongoing)

This is **not a task** — it's the ongoing process for pulling Vibe-Trading updates:

```bash
# 1. Fetch latest from upstream
cd ~/Documents/GitHub/Vibe-Trading
git fetch upstream

# 2. Update your main branch
git checkout main
git rebase upstream/main
git push origin main

# 3. If you have modifications on integration branch:
git checkout integration/adapter
git rebase main
# Resolve any conflicts (should be minimal if adapter is external)
git push origin integration/adapter --force-with-lease

# 4. Reinstall deps in MetaTraderBridge (if Vibe-Trading added new deps)
cd ~/Documents/GitHub/MetaTraderBridge
poetry install
```

**Why conflicts will be minimal:** The adapter lives in MetaTraderBridge, not Vibe-Trading. The only potential conflicts are if Vibe-Trading changes its MCP tool signatures or API endpoints — which the tests will catch immediately.

---

## What You Get After This Plan

| Capability | Before | After |
|---|---|---|
| Multi-market research (A-shares, HK/US, crypto) | ❌ MT5 only | ✅ Via Vibe-Trading skills |
| AI strategy generation | ❌ Manual only | ✅ 17 strategy skills via MCP |
| Backtesting with statistical validation | ❌ None | ✅ 7 engines + Monte Carlo + walk-forward |
| Multi-agent swarm debate | ❌ None | ✅ 29 team presets |
| Sentiment/flow analysis | ❌ None | ✅ Web search, SEC filings, ETF flow skills |
| Signal → MT5 execution | Manual translation | ✅ Automated via signal translator |
| Vibe-Trading updates | N/A | ✅ One `git rebase upstream/main` |

---

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| Vibe-Trading changes MCP tool signatures | Tests in Task 8 catch this; adapter layer isolates breakage |
| Vibe-Trading adds incompatible deps | Poetry resolves conflicts; if unresolvable, use subprocess with separate venv |
| Upstream update breaks integration branch | Rebase onto main, resolve conflicts in only-modified files |
| Vibe-Trading subprocess consumes too much memory | Lifecycle manager includes health checks; gateway route `/vibe/status` for monitoring |
| SSE communication unreliable | Client falls back to REST API calls; configurable timeout |
