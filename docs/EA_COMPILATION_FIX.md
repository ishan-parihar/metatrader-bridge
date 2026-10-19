# EA Compilation Fix

## Issues Fixed

### 1. SymbolSelect() Wrong Parameter Count (Line 116)
**Error**: `wrong parameters count - built-in: bool SymbolSelect(const string,bool)`

**Fix**: Changed from `SymbolSelect(symbol)` to `SymbolSelect(symbol, true)`

The MT5 `SymbolSelect()` function requires 2 parameters:
- `symbol` - The symbol name
- `select` - true to select, false to deselect

```mql5
// BEFORE (WRONG)
if(SymbolSelect(symbol))
   return true;

// AFTER (CORRECT)
if(SymbolSelect(symbol, true))
   return true;
```

### 2. Type Conversion Warnings (Lines 229, 235, 245, 252, 262, 276, 282, 289)
**Error**: `possible loss of data due to type conversion from 'double' to 'int'` and `implicit conversion from 'unknown' to 'string'`

**Fix**: Added explicit casts to all integer variables in StringFormat calls

Examples:
```mql5
// BEFORE (WARNINGS)
return StringFormat("{\"error\":\"copy_buffer_failed\",\"main\":%d,\"signal\":%d}", c1, c2);
return StringFormat("{\"indicator\":\"macd\",\"fast\":%d,\"slow\":%d,\"signal\":%d,...}", fast, slow, signal, ...);

// AFTER (CORRECT)
return StringFormat("{\"error\":\"copy_buffer_failed\",\"main\":%d,\"signal\":%d}", (int)c1, (int)c2);
return StringFormat("{\"indicator\":\"macd\",\"fast\":%d,\"slow\":%d,\"signal\":%d,...}", (int)fast, (int)slow, (int)signal, ...);
```

## Compilation Status

After these fixes, the EA should compile with **0 errors and 0 warnings**.

## How to Recompile

1. Open **MetaEditor** in MT5 (F4)
2. Open `BridgeConnectorEA.mq5`
3. Click **Compile** (F7)
4. Verify "0 errors, 0 warnings" in compilation log

## How to Deploy

1. After successful compilation, the `.ex5` file is automatically created in:
   ```
   <MT5 Data Folder>\MQL5\Experts\BridgeConnectorEA.ex5
   ```

2. Attach to chart:
   - Open any chart in MT5
   - Drag `BridgeConnectorEA.ex5` from Navigator → Expert Advisors
   - Or: Insert → Expert Advisors → BridgeConnectorEA

3. Configure EA settings:
   - ✓ Allow WebRequest
   - Add URL: `http://127.0.0.1:8020`
   - GatewayURL: `http://127.0.0.1:8020/bridge/terminal/heartbeat` (default)
   - HeartbeatSeconds: `5` (default)

4. Verify EA is running:
   - Blue icon should appear in top-right corner of chart
   - Check "Experts" tab in Toolbox for heartbeat messages
   - Test: `curl -s http://127.0.0.1:8020/bridge/terminal/status`

## Files Modified

- `ea/BridgeConnectorEA.mq5`
  - Line 116: Fixed `SymbolSelect()` call
  - Lines 222-290: Fixed type conversions in `StringFormat()` calls

## Next Steps

Once EA is compiled and attached:

1. **Add symbols to Market Watch**:
   - Ctrl+M to open Market Watch
   - Right-click → "Show All"
   - Ensure XAUUSDm is visible

2. **Test data retrieval**:
   ```bash
   curl -sX POST http://127.0.0.1:8010/tools/get_bars \
     -H 'Content-Type: application/json' \
     -d '{"symbol":"XAUUSD","timeframe":"H1","count":100}' | jq .
   ```

3. **Start trading analysis**:
   - Once data flows, I'll provide professional XAUUSD analysis
   - Multi-timeframe technical analysis
   - Trade setups with entry/SL/TP
   - Execute trades via MCP tools

## Technical Notes

### Why SymbolSelect(symbol, true)?

The second parameter controls whether to select or deselect the symbol:
- `true` - Add symbol to Market Watch
- `false` - Remove symbol from Market Watch

This ensures the symbol is available for data requests even if EA is attached to a different chart.

### Why Explicit Casts?

MQL5 is strict about type safety. When using `StringFormat()`:
- `%d` expects `int` type
- `%G` expects `double` type
- `%s` expects `string` type

Variables from `CopyBuffer()` return counts as `int` but compiler treats them as generic types, requiring explicit casts to avoid warnings.
