#!/bin/bash
# Setup script for XAUUSD trading with MetaTraderBridge bridge
# This script helps diagnose and fix common issues

echo "=== MetaTraderBridge Bridge Diagnostic Tool ==="
echo ""

# Check if services are running
echo "1. Checking MCP Server status..."
if curl -s http://127.0.0.1:8010/health > /dev/null 2>&1; then
    echo "   ✓ MCP Server is running on port 8010"
else
    echo "   ✗ MCP Server is NOT running"
    echo "   Start with: cd /home/ishanp/Documents/GitHub/MetaTraderBridge && .venv/bin/python -m uvicorn apps.mcp_server.main:app --host 127.0.0.1 --port 8010"
fi

echo ""
echo "2. Checking Bridge Gateway status..."
if curl -s http://127.0.0.1:8020/bridge/terminal/status > /dev/null 2>&1; then
    TERMINAL_STATUS=$(curl -s http://127.0.0.1:8020/bridge/terminal/status | jq -r '.connected')
    if [ "$TERMINAL_STATUS" = "true" ]; then
        echo "   ✓ Bridge Gateway is running and EA is connected"
    else
        echo "   ⚠ Bridge Gateway is running but EA is NOT connected"
        echo "   ACTION: Attach BridgeConnectorEA.mq5 to a chart in MT5"
    fi
else
    echo "   ✗ Bridge Gateway is NOT running"
    echo "   Start with: cd /home/ishanp/Documents/GitHub/MetaTraderBridge && .venv/bin/python -m uvicorn apps.bridge_gateway.main:app --host 127.0.0.1 --port 8020"
fi

echo ""
echo "3. Testing symbol normalization (XAUUSD -> XAUUSDm)..."
RESPONSE=$(curl -sX POST http://127.0.0.1:8010/tools/get_bars \
    -H 'Content-Type: application/json' \
    -d '{"symbol":"XAUUSD","timeframe":"H1","count":5}')
    
if echo "$RESPONSE" | jq -e '.symbol == "XAUUSD"' > /dev/null 2>&1; then
    echo "   ✓ Symbol normalization is working"
else
    echo "   ✗ Symbol normalization failed"
fi

echo ""
echo "4. Testing data retrieval..."
BARS_COUNT=$(echo "$RESPONSE" | jq '.data | length')
if [ "$BARS_COUNT" -gt 0 ] 2>/dev/null; then
    echo "   ✓ Successfully retrieved $BARS_COUNT bars for XAUUSD"
else
    echo "   ✗ No bars data returned for XAUUSD"
    echo ""
    echo "   TROUBLESHOOTING STEPS:"
    echo "   a) In MT5 Terminal:"
    echo "      - Open Market Watch (Ctrl+M)"
    echo "      - Right-click → Show All (or add XAUUSD/XAUUSDm manually)"
    echo "      - Open H1 chart for XAUUSDm"
    echo "      - Attach BridgeConnectorEA.mq5 to the XAUUSDm chart"
    echo ""
    echo "   b) Verify WebRequest permissions in MT5:"
    echo "      - Tools → Options → Expert Advisors"
    echo "      - Check 'Allow WebRequest for listed URL'"
    echo "      - Add: http://127.0.0.1:8020"
    echo ""
    echo "   c) Check if market is open:"
    echo "      - Gold market is closed on weekends"
    echo "      - Try EURUSD instead: curl -sX POST http://127.0.0.1:8010/tools/get_bars -H 'Content-Type: application/json' -d '{\"symbol\":\"EURUSD\",\"timeframe\":\"H1\",\"count\":5}'"
fi

echo ""
echo "5. Current account status..."
ACCOUNT=$(curl -s http://127.0.0.1:8010/resources/account/summary | jq -r '.environment // "unknown"')
echo "   Environment: $ACCOUNT"

echo ""
echo "=== Diagnostic Complete ==="
echo ""
echo "Quick Start Commands:"
echo "  Get XAUUSD H1 bars:  curl -sX POST http://127.0.0.1:8010/tools/get_bars -H 'Content-Type: application/json' -d '{\"symbol\":\"XAUUSD\",\"timeframe\":\"H1\",\"count\":100}'"
echo "  Get XAUUSD RSI:      curl -sX POST http://127.0.0.1:8010/tools/get_indicator -H 'Content-Type: application/json' -d '{\"symbol\":\"XAUUSD\",\"timeframe\":\"H1\",\"indicator\":\"rsi\",\"period\":14}'"
echo "  Get Account Info:    curl -s http://127.0.0.1:8010/tools/get_account_summary"
echo ""
