# Session Memory: TradeBridge Dockerization & Optimization (2026-05-06)

## Failures / Corrections

### Docker compose `version` attribute is obsolete in Compose v2
- **Category:** tool-quirk
- **What:** Left `version: "3.9"` in docker-compose.yml
- **Why it failed:** Docker Compose v2 (now the default) emits: "the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion"
- **What worked:** Remove the `version` line entirely from docker-compose.yml. Compose v2 ignores it and uses the Compose Specification directly.

### Poetry 2.x dropped `--no-update` and `--without` flags
- **Category:** correction
- **What:** Tried `poetry install --no-update` and `poetry install --without dev`
- **Why it failed:** Poetry 2.3.4 removed these flags. `--no-update` gave "The option '--no-update' does not exist". `--without` gave "The option '--without' does not exist". Poetry 2.x changed CLI API vs 1.x.
- **What worked:** Poetry 2.x uses different syntax for dependency groups. For path dependency issues, remove the problematic dependency from pyproject.toml first.

### Path dependency blocks lock/install when target directory missing
- **Category:** failure
- **What:** `vibe-trading-ai = { path = "../Vibe-Trading", develop = true }` in pyproject.toml
- **Why it failed:** Both `poetry lock` and `poetry install` hard-fail with "Path ... does not exist" when the sibling directory isn't cloned locally.
- **What worked:** Removed or commented out the path dependency from pyproject.toml before running lock/install.

### feedparser missing from pyproject.toml kills Docker MCP server
- **Category:** correction
- **What:** Docker MCP server container crashed on startup.
- **Why it failed:** `src/mt5_mcp/services/news_service.py` imports `feedparser` but it wasn't declared in `pyproject.toml`. Poetry lock didn't include it, so Docker pip install didn't have it. Runtime `ImportError` killed uvicorn.
- **What worked:** Added `feedparser` to `[tool.vibe-trading]` extra dependency group in pyproject.toml, ran `poetry lock` to update lockfile, rebuilt Docker image.

### VibeBridgeLifecycle crashes on missing Vibe-Trading directory
- **Category:** correction
- **What:** Gateway FastAPI app crashed at startup with AttributeError on None.
- **Why it failed:** `get_vibe_trading_dir()` returns `Path | None` (returns None when Vibe-Trading dir doesn't exist), but `VibeBridgeLifecycle.__init__()` didn't guard — `self._vibe_dir = get_vibe_trading_dir()` then code chained `.resolve()` and `.upper()` on a `None` Path, causing `AttributeError: 'NoneType' object has no attribute 'upper'`.
- **What worked:** Made `get_vibe_trading_dir()` explicitly return `Path | None`, made `VibeBridgeLifecycle.__init__()` check for None and set `self._vibe_dir = None`, gated auto-start behind `VIBE_TRADING_AUTO_START` env var check.
- **Pattern:** Optional external subprocess dependencies must tolerate missing targets at startup — check availability, degrade gracefully, gate with env var.

### Docker health check must use GET, not POST
- **Category:** correction
- **What:** Docker compose health check kept failing for HTTP gateway.
- **Why it failed:** Compose health check endpoint was `/bridge/health` which only responds to POST. Health check uses GET, getting 405 Method Not Allowed → `exit 1` → container marked unhealthy.
- **What worked:** Added a simple GET `/bridge/ping` endpoint returning `{"status": "ok"}` to the gateway. Used `curl -f http://localhost:8020/bridge/ping` as the health check command.

### asyncio.DEVNULL / asyncio.PIPE don't exist as module attributes
- **Category:** correction
- **What:** Used `asyncio.DEVNULL` and `asyncio.PIPE` in a previous session for Python subprocess.
- **Why it failed:** Python 3.12 doesn't have these as `asyncio` module attributes.
- **What worked:** `asyncio.subprocess.DEVNULL` and `asyncio.subprocess.PIPE`.

### Multi-stage Docker build slims Python ASGI images
- **Category:** insight
- **What:** Built a multi-stage Dockerfile for Python ASGI/MCP services (FastAPI, uvicorn, FastMCP).
- **Why it works:** Stage 1 (build) installs pip deps + builds any C extensions. Stage 2 (runtime) copies only `site-packages`, app code, and the Python runtime from the same base image. No build tools, no cache, no source tarballs in the final image.
- **Result:** Single image ~304 MB for all 3 services (tcp-bridge, http-gateway, mcp-server) vs separate images per service.
- **Best practice for Python MCP/HTTP services:**
  - Use `python:3.x-slim` as base (reduces image size significantly)
  - Build stage: `pip install` with `--no-cache-dir`, create a constraints/requirements file from poetry export
  - Runtime stage: copy only `site-packages` + app code + entrypoint
  - Use build args (`ARG TARGET`) to produce one image for multiple service variants
  - Health checks: TCP port check for TCP services, HTTP health endpoint for HTTP services
  - Resource reservations: set limits at ~2-5x actual RSS (e.g., 128M reservation for 49 MB actual, 256M limit for burst headroom)
  - `.dockerignore` must exclude: `.venv`, `__pycache__`, `.git`, `.benchmarks`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.coverage`, `*.log`, `.env`

## TradeBridge Actual Docker Memory Footprint
| Service | RSS | Image Size |
|---|---|---|
| TCP Bridge | 22 MB | 304 MB (shared image) |
| HTTP Gateway | 57 MB | 304 MB (shared image) |
| MCP Server | 80 MB | 304 MB (shared image) |
| Redis | 9 MB | 97 MB |
| **Total** | **168 MB** | |

## User Profile Updates
- GitHub: ishan-parihar
- MCP projects: LinkedIn MCP, Instagram MCP, Twitter MCP, TradeBridge
- Pushes to both GitHub and GitLab
- VPS: racknerd (109.199.106.105), accessed via `ssh-racknerd.sh` (sshpass-based)
- MCP projects live in `~/mcp/` on VPS
- Uses Hermes (`~/.hermes/config.yaml`) for MCP server orchestration
- Instagram MCP: personal fork `instagram-httpx-mcp` (HTTPX, uv-managed)
- Values: clean commits, documentation updates, methodical testing, production-readiness
- Memory-conscious: asks about runtime RSS before deploying to cheap VPS
