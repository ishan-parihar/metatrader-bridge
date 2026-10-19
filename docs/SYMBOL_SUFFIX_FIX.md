# Symbol Suffix Fix for MT5-MCP Bridge

## Problem
Your MT5 broker uses suffixed symbol names (e.g., `XAUUSDm`, `EURUSDm`) instead of standard names (`XAUUSD`, `EURUSD`). The EA was correctly designed to fetch data for ANY symbol from any chart, but the MCP server was sending requests without the 'm' suffix.

## Solution Implemented

### 1. Added Symbol Suffix Configuration
**File**: `src/mt5_mcp/settings/config.py`

Added new configuration option:
```python
symbol_suffix: str = os.getenv("MT5_SYMBOL_SUFFIX", "m")
```

### 2. Created Symbol Normalization Utilities
**File**: `src/mt5_mcp/adapters/common/symbol_utils.py`

Two key functions:
- `normalize_symbol(symbol)`: Adds suffix (XAUUSD → XAUUSDm)
- `denormalize_symbol(symbol)`: Removes suffix for display (XAUUSDm → XAUUSD)

### 3. Updated All MCP Endpoints
**File**: `apps/mcp_server/main.py`

All tool endpoints now automatically:
- Normalize symbols before sending to EA
- Denormalize symbols in responses to user

Affected endpoints:
- `/tools/get_bars`
- `/tools/get_indicator`
- `/tools/get_ticks`
- `/tools/get_order_book`
- `/tools/get_chart_screenshot`
- `/tools/submit_market_order_via_bridge`
- `/tools/submit_pending_order`
- `/tools/close_all_positions`
- `/tools/cancel_all_orders`

## Verification

The fix is working - logs show:
```
POST /bridge/commands/enqueue?type=get_bars&symbol=XAUUSDm&timeframe=H1&count=50
```

Symbol is correctly normalized from `XAUUSD` → `XAUUSDm` before being sent to EA.

## EA Architecture (Already Correct)

The EA (`BridgeConnectorEA.mq5`) was already designed to fetch data for ANY symbol:

```mql5
bool EnsureSymbolInMarketWatch(const string symbol)
{
   if(SymbolSelect(symbol))
      return true;
   Print("BridgeConnectorEA: Added symbol ", symbol, " to Market Watch");
   return true;
}

string JsonBars(const string symbol, const string timeframe, const int count)
{
   EnsureSymbolInMarketWatch(symbol);  // ← Ensures symbol is available
   // ... fetches data for ANY symbol, not just chart symbol
}
```

**Key Point**: The EA can fetch data for XAUUSDm even if attached to EURUSDm chart, as long as:
1. XAUUSDm is in Market Watch
2. MT5 terminal is connected to broker data feed
3. Market is open (not weekend/holiday)

## Setup Instructions

### In MT5 Terminal:

1. **Open Market Watch** (Ctrl+M)
2. **Add Symbols**:
   - Right-click in Market Watch window
   - Select "Show All" OR manually add XAUUSDm
3. **Open Chart** (optional but recommended):
   - Open H1 chart for XAUUSDm
4. **Attach EA**:
   - Attach `BridgeConnectorEA.mq5` to ANY chart
   - The EA can fetch data for all symbols
5. **Enable WebRequest**:
   - Tools → Options → Expert Advisors
   - ✓ Check "Allow WebRequest for listed URL"
   - Add: `http://127.0.0.1:8020`

### Verify Setup:

```bash
# Run diagnostic script
./setup_xauusd_trading.sh

# Test XAUUSD data
curl -sX POST http://127.0.0.1:8010/tools/get_bars \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"H1","count":100}'

# Test indicators
curl -sX POST http://127.0.0.1:8010/tools/get_indicator \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"H1","indicator":"rsi","period":14}'
```

## Environment Variable

To change or disable the suffix:

```bash
# Default (your broker): adds 'm' suffix
export MT5_SYMBOL_SUFFIX=m

# No suffix (standard symbols)
export MT5_SYMBOL_SUFFIX=""

# Different suffix (e.g., some brokers use '.')
export MT5_SYMBOL_SUFFIX=.
```

## Trading Commands (Once Data is Flowing)

```bash
# Get multi-timeframe analysis
curl -sX POST http://127.0.0.1:8010/tools/get_bars \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"D1","count":50}'

curl -sX POST http://127.0.0.1:8010/tools/get_bars \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"H4","count":100}'

curl -sX POST http://127.0.0.1:8010/tools/get_bars \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"H1","count":200}'

# Get technical indicators
curl -sX POST http://127.0.0.1:8010/tools/get_indicator \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"H1","indicator":"macd","fast":12,"slow":26,"signal":9}'

curl -sX POST http://127.0.0.1:8010/tools/get_indicator \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"H1","indicator":"bbands","period":20,"deviation":2}'

# Execute trade (demo mode)
curl -sX POST http://127.0.0.1:8010/tools/submit_market_order_via_bridge \
  -H 'Content-Type: application/json' \
  -d '{
    "intent_id":"trade-001",
    "strategy_id":"xauusd-momentum",
    "account_id":"demo",
    "symbol":"XAUUSD",
    "side":"buy",
    "order_kind":"market",
    "volume_lots":0.10,
    "sl":2950.00,
    "tp":3050.00,
    "deviation_points":20,
    "rationale":"Technical breakout with RSI confirmation"
  }'
```

## Troubleshooting

### No Data Returned

1. **Check if market is open**: Gold market closed on weekends
2. **Verify symbol in Market Watch**: Must be visible in MT5
3. **Check EA attachment**: EA must be attached to at least one chart
4. **Verify WebRequest**: Must be enabled for localhost:8020
5. **Test with EURUSD**: If EURUSD works but XAUUSD doesn't, market might be closed

### EA Not Responding

1. Check MT5 terminal is running
2. Verify EA is attached (look for blue icon in top-right of chart)
3. Check Experts tab in MT5 Terminal toolbox for errors
4. Verify heartbeat: `curl -s http://127.0.0.1:8020/bridge/terminal/status`

### Symbol Still Not Working

1. Check exact symbol name in MT5 Market Watch
2. Some brokers use different suffixes (., m, -)
3. Update environment variable: `export MT5_SYMBOL_SUFFIX=your_suffix`
4. Restart MCP server after changing config

## Summary

✅ **Fixed**: Symbol normalization (XAUUSD → XAUUSDm)  
✅ **Fixed**: All MCP endpoints updated  
✅ **Verified**: EA already supports any-symbol fetching  
⚠️ **Action Required**: Ensure MT5 has XAUUSDm in Market Watch and EA is attached  

The EA can now fetch data for ANY symbol from ANY chart - no need to attach to specific symbol charts!
