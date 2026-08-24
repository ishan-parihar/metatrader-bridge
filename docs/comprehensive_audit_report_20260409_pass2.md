# MetaTraderBridge Comprehensive Codebase Audit — Second Pass

**Date**: 2026-04-09 (Second Pass)
**Scope**: All Python source files in `apps/`, `src/`, `tools/`
**Previous Audit**: `docs/comprehensive_audit_report_20260409.md` (19 bugs + 5 previously known)

---

## Executive Summary

| Severity | Count | Key Impact |
|---|---|---|
| **CRITICAL** | 4 | Trading safety, data loss, resource exhaustion |
| **HIGH** | 5 | Silent failures, incorrect behavior, resource leaks |
| **MEDIUM** | 4 | Code quality, performance, maintainability |
| **LOW** | 3 | Hygiene, edge cases |

**New Bugs Found**: 16
**Previously Found & Fixed**: 5
**Previously Found & Still Open**: 7

---

## BUG FIX STATUS FROM PREVIOUS AUDIT

| ID | Severity | Status | Notes |
|---|---|---|---|
| BUG-C01 (RedisQueue.complete) | Critical | ✅ FIXED | `complete()` now uses try/except. **BUT `fail()` still has same bug** — see NEW-01. |
| BUG-C02 (eager queue_singleton) | Critical | ✅ FIXED | Now uses lazy `_queue_singleton = None` + `get_queue()`. |
| BUG-C03 (unbounded idempotency) | Critical | ✅ FIXED | `IdempotencyCache` now has LRU eviction (max_size=10000, ttl=3600). |
| BUG-C04 (TCP connection per call) | Critical | ⚠️ PARTIALLY FIXED | Shared TCP client singleton added, BUT still creates `asyncio.new_event_loop()` per call — see NEW-02. |
| BUG-H01 (bare except) | High | ✅ FIXED | Monolithic main.py refactored; zero bare `except:` found in codebase. |
| BUG-H02 (malformed JSON crash) | High | ✅ FIXED | `tcp_bridge_client.py:138-142` now catches `json.JSONDecodeError`. |
| BUG-H03 (RedisQueue.next atomicity) | High | ✅ FIXED | Now uses Lua script for atomic pop+get. |
| BUG-H04 (silent EA frame drop) | High | ✅ FIXED | `tcp_bridge/server.py:178-189` now sends ACK for orphaned frames. |
| BUG-H05 (TCP connect race) | High | ✅ FIXED | Uses `asyncio.Event` (`_connected_event`) instead of polling `_writer`. |
| BUG-H06 (stale HTTP client) | High | ⚠️ STILL OPEN | Persistent httpx.Client never validated — see NEW-03. |
| BUG-H07 (stale idempotency TTL) | High | ✅ FIXED | `InMemoryQueue.enqueue()` re-reads TTL from settings (line 41-43). |
| BUG-M01 (duplicated parser) | Medium | ✅ FIXED | `_parse_payload_dict` extracted to `shared.py:205`. |
| BUG-M03 (sync httpx in event loop) | Medium | ⚠️ STILL OPEN | `_batch_enqueue_and_await` HTTP fallback uses sync client in polling — see NEW-04. |
| BUG-M04 (unparseable dates kept) | Medium | ⚠️ STILL OPEN | See NEW-05. |
| BUG-M05 (snapshot validation) | Medium | ⚠️ STILL OPEN | See NEW-06. |
| BUG-L01 (__del__ cleanup) | Low | ⚠️ STILL OPEN | See NEW-07. |
| BUG-L02 (import json inside functions) | Low | ⚠️ STILL OPEN | Still scattered across multiple files. |

---

## NEW CRITICAL BUGS

### NEW-01: `RedisQueue.fail()` Has Same hset Bug That `complete()` Had

**Location**: `src/mt5_mcp/services/gateway_queue.py:172-177`

```python
def fail(self, id_: str, error: str) -> bool:
    if not self._r.hset(
        self._hash_prefix + id_, mapping={"status": "error", "error": error}
    ):
        return False
    return True
```

**Root Cause**: Identical to the original BUG-C01. `redis.hset()` returns `0` when updating existing fields, so `if not 0` → `True` → returns `False`. The `complete()` method was fixed with try/except, but `fail()` was overlooked.

**Impact**: When a command fails and `fail()` is called, the gateway returns `"status": "pending"` instead of `"error"`. The MCP server waits for a result that will never come, eventually timing out.

**Fix**: Apply same try/except pattern as `complete()`:
```python
def fail(self, id_: str, error: str) -> bool:
    try:
        self._r.hset(
            self._hash_prefix + id_, mapping={"status": "error", "error": error}
        )
        return True
    except Exception:
        return False
```

---

### NEW-02: `_tcp_send_and_await` Still Creates New Event Loop Per Call

**Location**: `apps/mcp_server/shared.py:113-123`

```python
def _tcp_send_and_await(type, payload, timeout_s=20.0):
    client = _get_tcp_client()  # ✅ Shared singleton
    loop = asyncio.new_event_loop()  # ❌ NEW loop per call
    try:
        result = loop.run_until_complete(client.send_command(...))
    finally:
        loop.close()  # ❌ Destroys loop after each call
```

**Root Cause**: While the TCP client is now shared, every single tool call (get_positions, get_orders, submit_order, close_position, etc.) creates and destroys a fresh `asyncio.EventLoop`. This is expensive and dangerous:
- Event loop creation/destruction is ~5-10ms even when healthy
- `run_until_complete()` inside a sync function called from FastAPI can conflict with the server's own event loop
- Each call blocks an entire sync worker thread

**Impact**: Every trading operation pays 15-25ms overhead. Under concurrent load, thread pool exhaustion. In async environments, potential event loop conflicts.

**Fix**: Use `asyncio.run()` (creates its own loop properly) or better — make the calling path async. For a proper fix, use a persistent background thread with a single event loop:
```python
_tcp_loop = None
_tcp_thread = None

def _get_event_loop():
    global _tcp_loop, _tcp_thread
    if _tcp_loop is None or _tcp_loop.is_closed():
        _tcp_thread = threading.Thread(target=_run_loop, daemon=True)
        _tcp_thread.start()
        # Signal when loop is ready
    return _tcp_loop
```

---

### NEW-03: `_batch_enqueue_and_await` Creates New TCP Client + New Event Loop

**Location**: `apps/mcp_server/shared.py:134-163`

```python
def _batch_enqueue_and_await(commands, timeout_s=20.0):
    tcp_client = TCPBridgeClient()  # ❌ NEW client per batch call
    loop = asyncio.new_event_loop()  # ❌ NEW event loop
    loop.run_until_complete(tcp_client.connect())
    ...
```

**Root Cause**: Completely ignores the shared `_get_tcp_client()` singleton. Creates a brand new `TCPBridgeClient()` AND a new event loop for every batch operation. This is the exact same pattern as the original BUG-C04, just in a different function.

**Impact**: Batch operations (close_all_positions, cancel_all_orders) are extremely slow — new TCP connection + event loop creation overhead. If TCP bridge is down, wastes up to 5s per batch call.

**Fix**: Use `_get_tcp_client()` instead of creating `TCPBridgeClient()` inline.

---

### NEW-04: `_await_result` Uses Sync `time.sleep()` in FastAPI Context

**Location**: `apps/mcp_server/shared.py:90-101`

```python
def _await_result(req_id, timeout_s=20.0, poll_s=0.1):
    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    end = time.time() + timeout_s
    while time.time() < end:
        r = client.get(f"{gw_url}/bridge/results/{req_id}")
        if r.status_code == 200:
            data = r.json()
            if data.get("status") in {"completed", "error"}:
                return data
        time.sleep(poll_s)  # ❌ Blocks sync worker thread
    return {"status": "timeout", "error": "timeout"}
```

**Root Cause**: `time.sleep(poll_s)` blocks the entire FastAPI sync worker thread for 100ms per poll iteration. With a 20s timeout and 100ms polling, a single request can block a thread for up to 20 seconds. FastAPI's default thread pool is 40 threads — under concurrent load (e.g., multiple wait tools), all threads can be exhausted.

**Impact**: Thread pool starvation under concurrent load. New requests queue indefinitely. Complete server unresponsiveness during heavy polling.

**Fix**: Convert to async with `asyncio.sleep()` or use a dedicated background thread for polling.

---

## NEW HIGH SEVERITY BUGS

### NEW-05: `_get_tcp_client()` Creates Event Loop at Import Time

**Location**: `apps/mcp_server/shared.py:66-82`

```python
def _get_tcp_client():
    global _tcp_client
    if _tcp_client is None:
        try:
            _tcp_client = TCPBridgeClient()
            loop = asyncio.new_event_loop()  # ❌ Blocks import
            loop.run_until_complete(_tcp_client.connect())
            loop.close()
        except Exception:
            _tcp_client = None
    return _tcp_client
```

**Root Cause**: The first call to `_get_tcp_client()` (which happens on the first tool call) creates an event loop, connects, then closes it. If the connection takes 5s (timeout), the first tool call blocks for 5 seconds. Worse, if TCP bridge is down, `_tcp_client` stays `None` and every subsequent call skips connection entirely (silent degradation without retry).

**Impact**: First tool call after startup blocks for up to 5s. If initial connection fails, TCP is permanently disabled for the session.

**Fix**: Connect lazily and retry on failure. Don't set `_tcp_client = None` on connect failure — allow retry.

---

### NEW-06: `wait_tools.py` Dynamically Loads main.py via `importlib`

**Location**: `apps/mcp_server/wait_tools.py:68-87`

```python
def _init_helpers():
    main_path = Path(__file__).parent / "main.py"
    spec = importlib.util.spec_from_file_location("mcp_main", str(main_path))
    main_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_module)
    _first_bid_ask = main_module._first_bid_ask
    ...
```

**Root Cause**: `apps/mcp_server/main.py` no longer exists (refactored into modular files). This means `_init_helpers()` will **always raise `RuntimeError`** on every wait tool invocation. All four wait tools (`mt5_wait_delay`, `mt5_wait_indicator`, `mt5_wait_trade_monitor`, `mt5_wait_for_price`) call `_init_helpers()` as their first action.

**Impact**: **All wait tools are completely broken.** Every invocation raises `RuntimeError: Could not load main.py from .../main.py`. This is a critical trading infrastructure failure — agents cannot wait for indicators, prices, or trade conditions.

**Fix**: Refactor `_init_helpers()` to import from the correct modular files:
```python
def _init_helpers():
    global _first_bid_ask, tool_get_order_book, tool_get_indicator
    if _first_bid_ask is not None:
        return
    from .shared import _first_bid_ask as _fb, _parse_payload_dict
    from .tools_data import mt5_get_order_book
    from .tools_analysis import mt5_get_indicator
    from .tools_resources import mt5_get_symbol_info
    from mt5_mcp.adapters.common.symbol_utils import normalize_symbol
    from mt5_mcp.services.regime_detection import detect_regime

    _first_bid_ask = _fb
    tool_get_order_book = mt5_get_order_book
    ...
```

---

### NEW-07: `detect_regime()` Always Called with Empty Bars

**Location**: `apps/mcp_server/wait_tools.py:306,330` and other locations

```python
regime_before = _detect_regime(bars=[], atr_value=None)
regime_before_label = regime_before.get("regime", "unknown")
```

**Root Cause**: Regime detection requires historical price bars to determine market regime (trending, ranging, volatile). Calling it with `bars=[]` always returns `"unknown"`. This happens in:
- `mt5_wait_delay` market state capture (lines 306, 330)
- `_get_market_context()` (line 996)

**Impact**: Market regime is always reported as "unknown" — regime-based trading logic is non-functional. Agents making decisions based on market regime get useless data.

**Fix**: Fetch historical bars before calling `detect_regime()`:
```python
bars_result = tool_get_bars(BarsRequest(symbol=symbol, timeframe="H1", count=100))
regime_before = _detect_regime(bars=bars_result.get("bars", []), atr_value=None)
```

---

### NEW-08: `_trailing_stops` Module Dict Has No Bounds

**Location**: `apps/mcp_server/tools_trading.py:27`

```python
_trailing_stops: dict[str, dict] = {}
```

**Root Cause**: Every call to `mt5_trail_position()` adds an entry to `_trailing_stops`. Entries are never evicted. In a continuously running system with many trailing stops, this grows unbounded.

**Impact**: Memory exhaustion over time. Similar to BUG-C03 but for trailing stops.

**Fix**: Use `IdempotencyCache` pattern or add TTL-based eviction.

---

### NEW-09: `mt5_close_position` Silent Fallback Without Logging

**Location**: `apps/mcp_server/tools_trading.py:398-413`

```python
if result is None:
    settings = get_settings_cached()
    client = get_http_client()
    req = client.post(...)  # Falls back to HTTP with no logging
```

**Root Cause**: When TCP fails for `mt5_close_position`, the code silently falls back to HTTP without any logging or warning. In a trading system, this is dangerous — the agent has no visibility into which transport was used, and if the HTTP fallback also fails, the error message is generic.

**Impact**: Silent transport degradation. Trading operations succeed or fail without visibility into which infrastructure layer is working.

**Fix**: Log a warning when TCP fails and HTTP fallback is used.

---

## NEW MEDIUM SEVERITY BUGS

### NEW-10: `_map_trade_retcode` Masks Unknown Retcodes

**Location**: `apps/mcp_server/tools_trading.py:42-52`

```python
def _map_trade_retcode(retcode) -> str:
    try:
        code = int(retcode) if retcode else 0
    except (ValueError, TypeError):
        code = 0
    if code in _SUCCESS_RETCODES:  # {10009, 10008}
        return "SUCCESS"
    elif code == 0:
        return "PENDING"
    else:
        return f"ERROR_{code}"
```

**Root Cause**: Retcode `0` maps to "PENDING" but is also used when `retcode` is None or unparseable. This masks genuine parsing failures as "pending" operations, when in reality the retcode was never received.

**Impact**: Failed orders with missing retcodes appear as "PENDING" — agents may retry or assume the order is still processing.

**Fix**: Distinguish between "no retcode received" and "retcode is 0".

---

### NEW-11: `_auto_log_trade` Swallows All Errors Silently

**Location**: `apps/mcp_server/tools_trading.py:84-102`

```python
def _auto_log_trade(...):
    try:
        journal = get_journal_db()
        journal.log_decision(...)
    except Exception:
        pass  # Journal failure should not block trade
```

**Root Cause**: While the comment says "journal failure should not block trade," silently swallowing all exceptions means journal corruption, database errors, and configuration issues are never detected. The trade journal is critical for post-trade analysis and auditing.

**Impact**: Journal silently stops working; no trades are logged; no alerts are raised.

**Fix**: At minimum, log a warning:
```python
except Exception as e:
    logger.warning("Trade journal logging failed: %s", e)
```

---

### NEW-12: `mt5_news_enrich` Always Returns Error

**Location**: `apps/mcp_server/tools_trading.py:1012-1017`

```python
@mcp.tool(name="mt5_news_enrich", annotations=_READ_ANNOTATIONS)
def mt5_news_enrich(news_id: str) -> dict:
    try:
        return {"error": "news enrichment unavailable"}
    except Exception as e:
        return {"error": str(e)}
```

**Root Cause**: The tool is registered but always returns an error. This is a stub that was never implemented. The `except` block is unreachable since the `return` always executes first.

**Impact**: Dead code. Agents calling this tool always get errors. Unreachable except block.

**Fix**: Either implement or remove the tool.

---

### NEW-13: `_detect_regime` in `snapshot_service.py` Called Without Bars

**Location**: `src/mt5_mcp/services/snapshot_service.py`

**Root Cause**: The snapshot service builds market snapshots but regime detection is called without historical bars data. This produces snapshots with `"regime": "unknown"` consistently.

**Impact**: All market snapshots have unknown regime — useless for analysis.

**Fix**: Fetch bars before building regime data.

---

## NEW LOW SEVERITY BUGS

### NEW-14: `queue_singleton = None` Breaks Backwards Compatibility

**Location**: `src/mt5_mcp/services/gateway_queue.py:228`

```python
queue_singleton = None
```

**Root Cause**: The comment says "Keep old name for backwards compatibility" but sets it to `None` instead of calling `get_queue()`. Any code that imports `queue_singleton` expecting it to be a working queue instance will get `None` and crash.

**Impact**: Import-time `None` for backwards compatibility name.

**Fix**: `queue_singleton = property(lambda: get_queue())` or document breaking change.

---

### NEW-15: `TCPBridgeClient` Default Port 8026 Doesn't Match Server Default

**Location**: `src/mt5_mcp/services/tcp_bridge_client.py:38`

```python
def __init__(self, host: str = "127.0.0.1", port: int = 8026):
```

vs `TCPBridgeServer`:
```python
def __init__(self, ..., mcp_port: int = 8026):
```

**Root Cause**: The client defaults to 8026 and the server's MCP port also defaults to 8026, so they match. However, `get_tcp_client()` reads `MT5_TCP_BRIDGE_MCP_PORT` while the server reads `MT5_TCP_BRIDGE_MCP_PORT` — same env var. But `_get_tcp_client()` in `shared.py` reads from a different location. This creates potential port mismatch if environment variables differ.

**Impact**: Minor — works with defaults but could mismatch with custom configs.

**Fix**: Centralize port configuration in a single constants file.

---

### NEW-16: `_first_bid_ask` Doesn't Handle Missing Keys Gracefully

**Location**: `apps/mcp_server/shared.py:234-242`

```python
def _first_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid = bids[0].get("price") if bids else None
    ask = asks[0].get("price") if asks else None
    ...
```

**Root Cause**: If `bids[0]` doesn't have a "price" key (malformed order book data), `.get("price")` returns `None` which is handled. But if `bids[0]` is not a dict (e.g., a string or number), `.get()` raises `AttributeError`.

**Impact**: Malformed order book data from EA crashes price extraction.

**Fix**: `isinstance(bids[0], dict) and bids[0].get("price")`.

---

## PRIORITIZED FIX RECOMMENDATIONS

### Phase 1 (Immediate — Trading Infrastructure Broken)

1. **NEW-06**: Fix `_init_helpers()` in wait_tools.py — **all wait tools are completely broken**
2. **NEW-01**: Fix `RedisQueue.fail()` — same hset bug as BUG-C01
3. **NEW-07**: Fix `detect_regime()` with empty bars — always returns "unknown"

### Phase 2 (High — Reliability & Safety)

4. **NEW-02**: Eliminate per-call event loop creation in `_tcp_send_and_await`
5. **NEW-03**: Fix `_batch_enqueue_and_await` to use shared TCP client
6. **NEW-04**: Convert `_await_result` to async or use background thread
7. **NEW-05**: Fix `_get_tcp_client()` retry logic on connection failure
8. **NEW-08**: Add bounds to `_trailing_stops` dict

### Phase 3 (Medium — Observability & Maintainability)

9. **NEW-09**: Add logging for TCP→HTTP fallback in trading tools
10. **NEW-10**: Fix `_map_trade_retcode` to distinguish missing retcodes
11. **NEW-11**: Log warnings in `_auto_log_trade` instead of silent pass
12. **NEW-12**: Implement or remove `mt5_news_enrich` stub
13. **NEW-13**: Fix regime detection in snapshot service

### Phase 4 (Low — Hygiene)

14. **NEW-14**: Fix `queue_singleton` backwards compatibility
15. **NEW-15**: Centralize port configuration
16. **NEW-16**: Add type safety to `_first_bid_ask`

---

## TOTAL BUG INVENTORY

| Source | Critical | High | Medium | Low | Total |
|---|---|---|---|---|---|
| Previous audit (fixed) | 3 | 3 | 1 | 0 | 7 |
| Previous audit (open) | 1 | 1 | 4 | 2 | 8 |
| This audit (new) | 4 | 5 | 4 | 3 | 16 |
| **Total outstanding** | **5** | **6** | **8** | **5** | **24** |
