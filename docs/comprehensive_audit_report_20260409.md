# MetaTraderBridge Comprehensive Codebase Audit — Full Bug Report

**Date**: 2026-04-09
**Scope**: All Python source files in `apps/`, `src/`, `tools/`
**Previously Known**: 5 bugs documented in `docs/waiting_tools_bug_report.md`

---

## Executive Summary

| Severity | Count | Key Impact |
|---|---|---|
| **CRITICAL** | 4 | Wrong results, data loss, memory exhaustion |
| **HIGH** | 7 | Silent failures, race conditions, lost commands |
| **MEDIUM** | 5 | Code duplication, performance degradation |
| **LOW** | 3 | Style, maintainability, edge cases |

**Total New Bugs**: 19 | **Previously Known**: 5

---

## CRITICAL

### BUG-C01: `RedisQueue.complete()` Always Returns `False` for Updates

**Location**: `src/mt5_mcp/services/gateway_queue.py:145-151`

```python
def complete(self, id_: str, result: dict[str, Any]) -> bool:
    if not self._r.hset(self._hash_prefix + id_,
        mapping={"status": "completed", "result": json.dumps(result)}):
        return False
    return True
```

**Root Cause**: `redis.hset()` returns the number of **newly added** fields. When updating an existing hash (the normal case), it returns `0`, causing `if not 0` → `True` → returns `False`.

**Impact**: The bridge gateway's `/bridge/results/{id}` endpoint (line 476) calls `queue.complete()` and relies on the return value. A `False` result means the gateway returns `"status": "pending"` even though the command completed, causing the MCP server to timeout waiting for results.

**Fix**:
```python
def complete(self, id_: str, result: dict[str, Any]) -> bool:
    try:
        self._r.hset(self._hash_prefix + id_,
            mapping={"status": "completed", "result": json.dumps(result)})
        return True
    except Exception:
        return False
```

---

### BUG-C02: `queue_singleton` Eager Initialization Blocks Startup

**Location**: `src/mt5_mcp/services/gateway_queue.py:209`

```python
queue_singleton = get_queue()  # Executes at import time
```

**Root Cause**: Module-level expression calls `get_queue()` which calls `_select_queue()` which attempts Redis connection with 0.5s socket timeout. If Redis is unavailable, every import blocks for 500ms.

**Impact**: Every process importing this module (MCP server, bridge gateway, tests) suffers 500ms startup delay. In CI/CD and dev environments without Redis, this compounds across all imports.

**Fix**: Remove eager initialization. Replace all `queue_singleton` references with `get_queue()`.

---

### BUG-C03: ExecutionGateway Idempotency Registry — Unbounded Memory Growth

**Location**: `src/mt5_mcp/services/execution_gateway/service.py:34,90-97`

**Root Cause**: `_idempotency_registry: dict[str, ExecutionResult] = {}` grows forever. No TTL, no max size, no eviction.

**Impact**: In a continuously running agentic trading system, every unique `idempotency_key` is stored permanently. Memory exhaustion over hours/days → OOM crash.

**Fix**: Implement LRU eviction or TTL-based cleanup. Suggested: `collections.OrderedDict` with max_size=10000 and TTL=3600s.

---

### BUG-C04: `_tcp_send_and_await` Creates New TCP Connection + Event Loop Per Call

**Location**: `apps/mcp_server/main.py:924-953`

```python
def _tcp_send_and_await(type, payload, timeout_s=20.0):
    tcp_client = TCPBridgeClient()       # New client every call
    loop = asyncio.new_event_loop()      # New event loop every call
    loop.run_until_complete(tcp_client.connect())  # Up to 5s
    ...
    loop.run_until_complete(tcp_client.close())
    loop.close()
```

**Root Cause**: No connection pooling. Every tool call (get_positions, get_orders, get_account, etc.) creates a fresh TCP connection and event loop, connects, sends one command, and tears down.

**Impact**:
- ~15-25ms overhead per call even when healthy
- Up to 5s wasted per call when TCP bridge is down
- Creates/destroys event loops in sync context — dangerous in async environments
- Each call blocks an entire sync worker thread

**Fix**: Use a shared persistent TCP client with lazy initialization, similar to `get_http_client()`.

---

## HIGH

### BUG-H01: Bare `except:` Catches `SystemExit` and `KeyboardInterrupt`

**Location**: `apps/mcp_server/main.py:1799,1864`

```python
try:
    retcode_int = int(retcode) if retcode else None
except:  # Catches SystemExit, KeyboardInterrupt, GeneratorExit
    retcode_int = None
```

**Impact**: Process shutdown signals (Ctrl+C, SIGTERM) are silently swallowed during order submission, making the server unresponsive to graceful shutdown.

**Fix**: `except (ValueError, TypeError):`

---

### BUG-H02: `TCPBridgeClient._recv_loop` Crashes on Malformed JSON

**Location**: `src/mt5_mcp/services/tcp_bridge_client.py:134`

```python
frame = json.loads(json_bytes)  # JSONDecodeError not caught
```

**Root Cause**: A single malformed JSON frame from the EA raises `json.JSONDecodeError` which propagates up to `_reconnect_loop`, caught by `except Exception`, triggering a full disconnect/reconnect cycle.

**Impact**: One corrupted frame → full reconnection → all pending commands fail.

**Fix**: Wrap JSON parse in try/except, log warning, `continue` to next frame.

---

### BUG-H03: `RedisQueue.next()` — Non-Atomic Pop Causes Command Loss

**Location**: `src/mt5_mcp/services/gateway_queue.py:127-143`

```python
def next(self) -> Optional[Command]:
    cmd_id = self._r.rpop(self._list_key)    # Step 1: remove from list
    ...
    h = self._r.hgetall(self._hash_prefix + cmd_id)  # Step 2: get details
    if not h:
        return None  # Command lost — already popped but hash gone
```

**Root Cause**: If the command hash expires (TTL=600s) between `rpop` and `hgetall`, the command is silently lost. Already removed from list, but details are gone.

**Impact**: Commands can be silently dropped under Redis memory pressure or TTL expiry.

**Fix**: Use a Lua script or Redis transaction (MULTI/EXEC) to atomically pop and retrieve.

---

### BUG-H04: `TCPBridgeServer._handle_frame` Silently Drops EA Results for Timed-Out Requests

**Location**: `apps/tcp_bridge/server.py:156-177`

```python
if request_id and request_id in self._pending:
    cmd = self._pending.pop(request_id)
    ...
else:
    logger.warning(f"Unsolicited frame from EA: {frame}")
    # Frame silently dropped — no ACK to EA
```

**Root Cause**: If the MCP server timed out waiting for a result and removed the request from `_pending`, the EA's eventual result is silently discarded with no acknowledgment.

**Impact**: EA executes command successfully but result is lost. EA has no way to know the result was received.

**Fix**: Send an ACK or error response back to the EA for unmatched frames.

---

### BUG-H05: `TCPBridgeClient.connect()` — Race Condition on `_writer` Check

**Location**: `src/mt5_mcp/services/tcp_bridge_client.py:78-86`

```python
async def connect(self, ...):
    self._reconnect_task = asyncio.create_task(self._reconnect_loop())
    for _ in range(50):
        if self._writer and self._writer.is_closing() is False:
            return
        await asyncio.sleep(0.1)
    raise ConnectionError(...)
```

**Root Cause**: `_writer` is only set inside `_reconnect_loop` after `open_connection()` succeeds. Under load or DNS latency, `connect()` may see `_writer` as None even though the connection is being established.

**Impact**: Intermittent false-negative connection failures under network latency.

**Fix**: Use `asyncio.Event` to signal successful connection.

---

### BUG-H06: `EABridgeAdapter._client` — No Health Check for Stale Connections

**Location**: `src/mt5_mcp/adapters/ea_bridge_adapter/adapter.py:55-64`

**Root Cause**: The persistent `httpx.Client` is created once and never validated. After a bridge gateway restart, stale Keep-Alive connections may be used for up to 30s (keepalive_expiry), causing all commands to fail during that window.

**Impact**: 30-second outage window after gateway restart.

**Fix**: Add health check before requests or implement automatic client recreation on connection error.

---

### BUG-H07: `InMemoryQueue._idempotency_ttl` Stale After Settings Change

**Location**: `src/mt5_mcp/services/gateway_queue.py:31`

```python
self._idempotency_ttl: float = float(get_settings().idempotency_ttl_seconds)
```

**Root Cause**: TTL read once at construction. If settings change at runtime, queue uses stale value.

**Impact**: Idempotency window doesn't respect runtime configuration changes.

**Fix**: Read TTL from settings on each enqueue call.

---

## MEDIUM

### BUG-M01: `_parse_payload_dict` Duplicated 3 Times

**Location**: `apps/mcp_server/main.py:3162,3397,3646`

**Root Cause**: The same nested function `_parse_payload_dict` is copy-pasted in 3 different endpoint functions. Any fix to one won't propagate to the others.

**Impact**: Maintenance burden, inconsistency risk, code bloat.

**Fix**: Extract to module-level utility function.

---

### BUG-M02: `news_service.py` Uses MD5 for ID Generation

**Location**: `src/mt5_mcp/services/news_service.py:339-341`

```python
def _hash_id(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]
```

**Root Cause**: MD5 is cryptographically broken. While this is only used for non-security ID generation, it's still a code smell.

**Impact**: Minimal — MD5 is fine for non-collision-sensitive ID generation. But modern alternatives exist.

**Fix**: Use `hashlib.sha256` or `hashlib.blake2b` for forward compatibility.

---

### BUG-M03: `_batch_enqueue_and_await` HTTP Fallback Uses Sync httpx in Event Loop Context

**Location**: `apps/mcp_server/main.py:1011-1037`

```python
client = get_http_client()  # sync httpx.Client
for cmd in commands:
    r = client.post(...)  # Blocks worker thread
    ...
_t.sleep(0.1)  # Blocks worker thread during polling
```

**Root Cause**: When TCP falls back to HTTP, the synchronous `httpx.Client` blocks the entire worker thread during both request and polling.

**Impact**: Under concurrent load, all sync workers can be blocked simultaneously, causing request queuing.

**Fix**: Use `httpx.AsyncClient` with `asyncio.gather` for parallel polling.

---

### BUG-M04: `news_service._filter_by_time` Keeps Items with Unparseable Dates

**Location**: `src/mt5_mcp/services/news_service.py:357-359`

```python
except (ValueError, TypeError):
    filtered.append(item)  # Keep items with unparseable dates (might be recent)
```

**Root Cause**: Items with unparseable dates are kept unconditionally. If a source consistently returns malformed dates, all its items pass through the time filter.

**Impact**: Very old news items with malformed dates appear as "recent" news.

**Fix**: Drop items with unparseable dates or add a fallback heuristic.

---

### BUG-M05: `snapshot_service.py` Doesn't Validate Input Data Integrity

**Location**: `src/mt5_mcp/services/snapshot_service.py:54-81`

**Root Cause**: The `build()` method accepts `bars_data`, `indicator_data`, `order_book_data`, etc. as optional parameters but doesn't validate that they're internally consistent (e.g., bars match the requested symbol/timeframe).

**Impact**: Caller can accidentally mix data from different symbols/timeframes, producing incorrect snapshots.

**Fix**: Add optional symbol/timeframe validation on input data.

---

## LOW

### BUG-L01: `data_store.py` Uses `__del__` for Cleanup

**Location**: `src/mt5_mcp/services/data_store.py:669-673`

```python
def __del__(self) -> None:
    try:
        self.close()
    except Exception:
        pass
```

**Root Cause**: `__del__` is not guaranteed to be called (e.g., during interpreter shutdown, circular references).

**Impact**: SQLite connections may not be cleanly closed on process exit.

**Fix**: Use context manager (`__enter__`/`__exit__`) or explicit `close()` in shutdown hooks.

---

### BUG-L02: `import json` Inside Functions (Repeated 15+ Times)

**Location**: `apps/mcp_server/main.py` (multiple locations)

**Root Cause**: `import json` is repeated inside many function bodies instead of at module level. While Python caches imports, this is still a code smell and makes the file harder to read.

**Impact**: Zero runtime impact, but code hygiene issue.

---

### BUG-L03: `_parse_payload_dict` Defined as Nested Function (3x)

**Location**: `apps/mcp_server/main.py:3162,3397,3646`

**Root Cause**: Three different endpoints each define their own local `_parse_payload_dict` function instead of using the module-level `_parse_payload` at line 893.

**Impact**: Code duplication, maintenance burden.

---

## PREVIOUSLY KNOWN BUGS (from waiting_tools_bug_report.md)

| ID | Severity | Component | Summary |
|---|---|---|---|
| BUG-001 | Critical | wait/delay | Silent exception during state capture — agent gets no error indication |
| BUG-002 | Critical | wait/indicator | `crosses` condition always returns `triggered=True` on first poll |
| BUG-003 | High | wait/indicator | Silent `except: pass` swallows all errors during indicator polling |
| BUG-005 | High | wait_for_price | Silent `except: pass` in price polling loop |

---

## PRIORITIZED FIX RECOMMENDATIONS

### Phase 1 (Immediate — Trading Safety)
1. **BUG-C01**: Fix `RedisQueue.complete()` — causes false "pending" status for completed commands
2. **BUG-H01**: Fix bare `except:` — prevents graceful shutdown
3. **BUG-002**: Fix `crosses` condition — false trading signals
4. **BUG-H03**: Fix `RedisQueue.next()` atomicity — command loss

### Phase 2 (High — Reliability)
5. **BUG-C03**: Add TTL/eviction to idempotency registry — memory exhaustion
6. **BUG-H02**: Handle malformed JSON in TCP recv loop — unnecessary reconnections
7. **BUG-H04**: Acknowledge unmatched EA frames — result loss
8. **BUG-C02**: Lazy-init `queue_singleton` — startup delay
9. **BUG-C04**: Persistent TCP client — performance and reliability

### Phase 3 (Medium — Maintainability)
10. **BUG-M01**: Deduplicate `_parse_payload_dict`
11. **BUG-M03**: Async HTTP fallback
12. **BUG-H05**: Race condition in TCP connect
13. **BUG-H06**: HTTP client health check
14. **BUG-H07**: Fresh idempotency TTL reads

### Phase 4 (Low — Hygiene)
15. **BUG-L01**: Replace `__del__` with context manager
16. **BUG-L02/L03**: Module-level imports, deduplicate parsers
