# Waiting Tools Bug Report

**Date**: 2026-04-09
**Project**: MetaTraderBridge
**Files Analyzed**: 
- `apps/mcp_server/main.py` (lines 4334-4393, 5441-5930)
- `src/mt5_mcp/schemas/tools.py` (lines 406-485)
- `tools/mcp_mt5_wrapper.py`
- `src/mt5_mcp/services/market_regime.py` (detect_regime)

---

## BUG-001: Double Sleep in `wait/delay` (CRITICAL)

**Location**: `apps/mcp_server/main.py`, lines 5477 and 5514
**Severity**: Critical
**Component**: `tool_wait_delay`

### Description
When a `symbol` is provided in the request, the code calls `await asyncio.sleep(req.duration_seconds)` TWICE:
1. **Line 5477**: Inside the `try` block after capturing pre-sleep state
2. **Line 5514**: In the `else` branch of the outer `if req.symbol:` conditional

The code structure is:
```python
if req.symbol:
    try:
        # ... capture pre-sleep state ...
        await asyncio.sleep(req.duration_seconds)  # <-- FIRST sleep (line 5477)
        # ... capture post-sleep state ...
    except Exception:
        market_summary = None
else:
    await asyncio.sleep(req.duration_seconds)  # <-- SECOND sleep (line 5514)
```

**The bug**: When `req.symbol` is provided and no exception occurs, the code executes line 5477 (first sleep) AND then falls through to return the result. However, when `symbol` is provided AND an exception occurs in the pre/post state capture (line 5510-5511), the `except` block runs (setting `market_summary = None`) and then execution continues to line 5516 (the return). The else branch on line 5512-5514 is only hit when `symbol` is NOT provided.

**Correction**: Upon closer inspection, the code structure actually does NOT double-sleep for the same request. The `if/else` is exclusive. However, there is a SECOND distinct issue: when an exception occurs inside the try block AFTER the sleep (e.g., during post-sleep state capture), the sleep has already happened, the except block sets `market_summary=None`, and execution returns normally. This is a data loss bug, not a double-sleep.

**Re-evaluation**: The originally reported "double sleep" is NOT a bug — the if/else structure is mutually exclusive. The real issue is that when the pre/post state capture fails (exception at lines 5450-5509), the sleep STILL happens (line 5477) but the agent gets `market_summary=None` with no indication that anything went wrong.

### Impact
- **Wait time**: Correct (no double sleep)
- **Data integrity**: When state capture fails, agent gets no market summary but no error indication
- **Debugging**: Silent exception at line 5510 (`except Exception: market_summary = None`)

### Recommended Fix
1. Add logging for exceptions in the state capture block
2. Include error information in the result when market_summary capture fails

---

## BUG-002: `wait/indicator` Crosses Condition Always Triggers (CRITICAL)

**Location**: `apps/mcp_server/main.py`, lines 5574-5582
**Severity**: Critical
**Component**: `tool_wait_for_indicator`

### Description
The `crosses` condition returns `triggered=True` on the FIRST poll iteration without actually detecting a crossover:

```python
elif req.condition == "crosses":
    return WaitForIndicatorResult(
        ...
        triggered=True,  # Always True, no crossing detection!
    )
```

A crossover requires tracking the PREVIOUS value and detecting when the value moves from one side of the threshold to the other. The current implementation:
1. Gets current indicator value
2. Immediately returns triggered=True
3. Never checks if a crossing actually occurred

### Impact
- Agent requests a crossover signal (e.g., RSI crosses above 30)
- Server immediately returns triggered=True on first poll
- Agent acts on false signal, potentially entering trades prematurely

### Recommended Fix
Track `previous_value` across poll iterations. Trigger only when:
- `(previous_value < threshold and current_value >= threshold)` OR
- `(previous_value >= threshold and current_value < threshold)`

---

## BUG-003: Silent `except: pass` in `wait/indicator` (HIGH)

**Location**: `apps/mcp_server/main.py`, lines 5583-5584
**Severity**: High
**Component**: `tool_wait_for_indicator`

### Description
```python
except Exception:
    pass
```

This bare except swallows ALL errors during indicator polling, including:
- Network failures to the MT5 bridge
- Invalid indicator names
- Symbol not found
- Timeout errors
- Any runtime exception

### Impact
- Impossible to debug why a wait condition isn't triggering
- Agent waits until timeout without knowing indicator fetch is failing
- Masks systemic issues (e.g., bridge disconnection)

### Recommended Fix
Log warnings with error details using the project's logging infrastructure.

---

## BUG-004: `wait_for_price` Crosses Condition Always Triggers (CRITICAL)

**Location**: `apps/mcp_server/main.py`, lines 4360-4363
**Severity**: Critical
**Component**: `tool_wait_for_price`

### Description
```python
else:  # crosses
    mid = (bid + ask) / 2
    current = mid
    triggered = True  # Any update is a "cross" in this mode
```

The comment "Any update is a 'cross' in this mode" reveals this was intentionally implemented incorrectly. Every price update triggers the alert immediately.

### Impact
- Price alert fires instantly on first poll instead of waiting for actual crossover
- Agent receives false trigger notification
- Equivalent to no condition at all

### Recommended Fix
Track `previous_price` across iterations. Trigger only when price actually crosses the threshold.

---

## BUG-005: Silent `except: pass` in `wait_for_price` (HIGH)

**Location**: `apps/mcp_server/main.py`, lines 4373-4374
**Severity**: High

### Description
Same pattern as BUG-003 — silent exception swallowing during price polling.

### Recommended Fix
Add logging for exceptions.

---

## BUG-006: No Duration Validation on `wait/delay` (MEDIUM)

**Location**: `apps/mcp_server/main.py`, line 5442; `src/mt5_mcp/schemas/tools.py`, line 451
**Severity**: Medium

### Description
`WaitDelayRequest.duration_seconds` is typed as `int` with default 60, but has no upper bound constraint. An agent could request 86400 seconds (24 hours), effectively blocking the HTTP connection indefinitely.

Contrast with `wait/trade_monitor` which properly enforces a 3600-second maximum (line 5651).

### Impact
- HTTP connection held open for arbitrary duration
- Resource exhaustion (memory, connection pool)
- Potential denial of service

### Recommended Fix
Add Pydantic `Field(ge=1, le=3600)` constraint or explicit validation.

---

## BUG-007: `TradeMonitorRequest` Defined Inline Instead of in Centralized Schema (LOW)

**Location**: `apps/mcp_server/main.py`, lines 5616-5622
**Severity**: Low

### Description
The `TradeMonitorRequest` class is defined inline in `main.py` instead of in `src/mt5_mcp/schemas/tools.py` with the other request models.

### Impact
- Schema inconsistency
- Harder to maintain and discover
- Cannot be reused by other modules

### Recommended Fix
Move to `src/mt5_mcp/schemas/tools.py`.

---

## BUG-008: `detect_regime` Called with Empty Data (MEDIUM)

**Location**: `apps/mcp_server/main.py`, lines 5466, 5491, 5745-5748, 5780-5783
**Severity**: Medium

### Description
`detect_regime(bars=[], atr_value=None)` and `detect_regime(bars=[], atr_value=atr_value)` are called with empty bars. Looking at the implementation in `market_regime.py` line 206:

```python
if not bars or atr_value <= 0:
    return {"regime": "unknown", "confidence": 0.0, ...}
```

This means regime detection ALWAYS returns "unknown" in the wait tools.

### Impact
- `market_summary.regime_before` and `regime_after` are always "unknown"
- Market context in trade_monitor always shows regime "unknown"
- Misleading to agent — suggests data is being used when it's not

### Impact on Migration
Note this in the new implementation but don't change behavior — fixing this requires fetching bars data which is out of scope.

---

## MCP SDK Compliance Issues

### ISSUE-001: No Official MCP Python SDK Usage

**Severity**: High

The project does NOT use the official MCP Python SDK (`mcp` package). It's not listed in `pyproject.toml` dependencies. The "MCP server" is a FastAPI app exposing HTTP POST endpoints, not a real MCP server.

### ISSUE-002: Manual TOOL_SPECS Duplication

**Location**: `tools/mcp_mt5_wrapper.py`, lines 375+

The wrapper manually defines `TOOL_SPECS` with descriptions and JSON schemas — this duplicates what the MCP SDK would auto-generate from Pydantic models and docstrings.

### ISSUE-003: No MCP Protocol Features

- No tool annotations (`readOnlyHint`, `destructiveHint`, etc.)
- No proper MCP tool registration
- No stdio or streamable HTTP transport via MCP SDK
- No structured output schemas
- The agent interacts via HTTP POST, not MCP protocol

---

## Summary

| ID | Component | Severity | Description |
|----|-----------|----------|-------------|
| BUG-001 | wait/delay | Critical | Silent exception masking, not double-sleep |
| BUG-002 | wait/indicator | Critical | Crosses always triggers immediately |
| BUG-003 | wait/indicator | High | Silent except: pass |
| BUG-004 | wait_for_price | Critical | Crosses always triggers immediately |
| BUG-005 | wait_for_price | High | Silent except: pass |
| BUG-006 | wait/delay | Medium | No upper bound on duration_seconds |
| BUG-007 | trade_monitor | Low | Inline request model instead of centralized |
| BUG-008 | All wait tools | Medium | detect_regime always returns "unknown" |
