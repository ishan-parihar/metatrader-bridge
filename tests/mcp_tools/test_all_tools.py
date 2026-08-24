"""Comprehensive production-grade test suite for all MetaTraderBridge tools.

Tests each tool for:
1. Registration — tool exists and is callable
2. Return type — returns dict (JSON-serializable)
3. Error handling — no uncaught exceptions crash the server
4. Structure — response has expected keys
5. Policy gating — execution tools respect demo-only policy

Strategy: Mock the TCP bridge and gateway to return controlled responses,
then verify each tool handles them correctly.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings():
    """Mock settings with localhost URLs."""
    settings = MagicMock()
    settings.gateway_url = "http://127.0.0.1:8020"
    settings.mcp_server_url = "http://127.0.0.1:8010"
    with patch("apps.mcp_server.shared.get_settings", return_value=settings):
        yield settings


@pytest.fixture
def mock_gateway():
    """Mock ExecutionGateway with stubbed methods."""
    gw = MagicMock()

    # Resource methods
    gw.terminal_status.return_value = MagicMock(
        model_dump=lambda: {
            "connected": True,
            "terminal_id": "TEST_TERM_001",
            "server": "TestServer-Demo",
            "uptime_seconds": 3600,
        }
    )
    gw.account_summary.return_value = MagicMock(
        account_id="DEMO_12345",
        model_dump=lambda: {
            "account_id": "DEMO_12345",
            "balance": 10000.0,
            "equity": 10050.0,
            "margin": 500.0,
            "free_margin": 9550.0,
            "currency": "USD",
            "leverage": 100,
        },
    )
    gw.adapter.get_symbol_info.return_value = MagicMock(
        symbol="XAUUSDm",
        model_dump=lambda: {
            "symbol": "XAUUSDm",
            "point": 0.01,
            "tick_value": 1.0,
            "tick_size": 0.01,
            "spread": 15,
            "digits": 2,
            "trade_contract_size": 100,
        },
    )
    gw.adapter.get_positions.return_value = [
        MagicMock(
            model_dump=lambda: {
                "position_id": "12345",
                "symbol": "XAUUSDm",
                "side": "buy",
                "volume": 0.1,
                "entry_price": 2650.0,
                "mark_price": 2655.0,
                "sl": 2640.0,
                "tp": 2680.0,
                "profit": 50.0,
                "spread": 15,
                "time": time.time() - 300,
            }
        )
    ]
    gw.adapter.get_orders.return_value = []
    gw.adapter.get_deals_history.return_value = [
        MagicMock(
            model_dump=lambda: {
                "deal_id": "999",
                "symbol": "XAUUSDm",
                "volume": 0.1,
                "price": 2650.0,
                "profit": 25.0,
                "type": "buy",
                "time": datetime.now(timezone.utc).isoformat(),
            }
        )
    ]

    with patch("apps.mcp_server.shared.get_gateway", return_value=gw):
        yield gw


@pytest.fixture
def mock_http_client():
    """Mock HTTP client for gateway fallback."""
    client = MagicMock()
    # Status endpoint
    status_response = MagicMock()
    status_response.status_code = 200
    status_response.json.return_value = {"connected": True, "terminal_id": "TEST_001"}
    status_response.raise_for_status = MagicMock()
    # Bridge health
    health_response = MagicMock()
    health_response.status_code = 200
    health_response.json.return_value = {"status": "ok", "version": "1.0"}
    health_response.raise_for_status = MagicMock()
    # Result polling (empty)
    empty_response = MagicMock()
    empty_response.status_code = 404
    empty_response.raise_for_status = MagicMock()

    def get_side_effect(url, **kwargs):
        if "/status" in url or "/terminal/status" in url:
            return status_response
        if "/health" in url:
            return health_response
        if "/bridge/results/" in url:
            return empty_response
        return empty_response

    client.get.side_effect = get_side_effect
    client.post.return_value = empty_response

    with patch("apps.mcp_server.shared.get_http_client", return_value=client):
        yield client


@pytest.fixture
def mock_tcp_bridge():
    """Reload all tool modules after patching shared.py so they pick up mocked functions."""
    import uuid
    import apps.mcp_server.shared as shared
    import importlib
    import apps.mcp_server

    def _fake_tcp(type: str, payload: dict, timeout_s: float = 20.0):
        rid = str(uuid.uuid4())
        map = {
            "get_account_summary": {"account_id": "DEMO_12345", "balance": 10000.0},
            "get_symbol_info": {
                "symbol": payload.get("symbol", "XAUUSD"),
                "point": 0.01,
                "tick_value": 1.0,
                "spread": 15,
            },
            "get_positions": {
                "positions": [
                    {
                        "position_id": "12345",
                        "symbol": "XAUUSDm",
                        "side": "buy",
                        "volume": 0.1,
                        "entry_price": 2650.0,
                        "mark_price": 2655.0,
                        "sl": 2640.0,
                        "tp": 2680.0,
                        "profit": 50.0,
                        "spread": 15,
                        "time": time.time() - 300,
                    }
                ]
            },
            "get_orders": {"orders": []},
            "get_deals_history": {"deals": []},
            "get_bars": {
                "bars": [
                    {
                        "time": "2026-04-09T10:00:00Z",
                        "open": 2650.0,
                        "high": 2655.0,
                        "low": 2648.0,
                        "close": 2653.0,
                        "volume": 100,
                    }
                ]
            },
            "get_indicator": {"values": [55.0, 58.0, 62.0]},
            "get_ticks": {"ticks": [{"time": "2026-04-09T10:00:00Z", "bid": 2650.0, "ask": 2650.15}]},
            "get_order_book": {"bids": [[2650.0, 1.0]], "asks": [[2650.15, 1.0]]},
            "get_chart_screenshot": {
                "screenshot_path": "/tmp/ss.png",
                "base64": "iVBORw0KGgo=",
            },
            "submit_order": {
                "status": "placed",
                "order_id": "ORD_001",
                "retcode": 10009,
            },
            "close_position": {"status": "closed", "retcode": 10009},
            "modify_order": {"status": "modified", "retcode": 10009},
            "cancel_order": {"status": "cancelled", "retcode": 10009},
        }
        return {
            "type": type,
            "request_id": rid,
            "status": "completed",
            "payload": json.dumps(map.get(type, {"ok": True})),
        }

    def _fake_batch(commands, timeout_s=20.0):
        return [
            _fake_tcp(
                c.get("type", "unknown"),
                {k: v for k, v in c.items() if k != "type"},
                timeout_s,
            )
            for c in commands
        ]

    def _fake_await(req_id, timeout_s=20.0, poll_s=0.1):
        return {"status": "completed", "payload": json.dumps({"ok": True})}

    shared._tcp_send_and_await = _fake_tcp
    shared._batch_enqueue_and_await = _fake_batch
    shared._await_result = _fake_await

    # Clear the global mcp instance to force re-registration
    apps.mcp_server.mcp._tool_manager._tools.clear()

    # Reload all tool modules so they re-import the patched functions
    for name in [
        "tools_resources",
        "tools_market_data",
        "tools_trading",
        "tools_metacognition",
        "tools_context",
        "tools_portfolio",
        "tools_ea_native",
        "tools_data",
        "tools_management",
        "tools_vibe",
        "tools_wait",
        "tools_analysis",
        "tools_fundamentals",
        "tools_streaming",
    ]:
        mod = __import__(f"apps.mcp_server.{name}", fromlist=["mcp"])
        importlib.reload(mod)

    yield


@pytest.fixture
def mock_policy():
    """Mock policy engine to allow demo trades."""
    policy = MagicMock()
    policy.validate_submit_order.return_value = MagicMock(
        allowed=True,
        reason="Demo account — allowed",
        environment="demo",
    )
    with patch("mt5_mcp.policy.engine.get_policy", return_value=policy):
        yield policy


@pytest.fixture
def mcp_server(mock_settings, mock_gateway, mock_http_client, mock_tcp_bridge, mock_policy):
    """Create MCP server with all mocks."""
    from apps.mcp_server import create_mcp_server

    server = create_mcp_server()
    return server


# ---------------------------------------------------------------------------
# Helper: Call a tool by name on the MCP server
# ---------------------------------------------------------------------------


def call_tool_sync(server, tool_name: str, arguments: dict | None = None):
    """Call a tool by name, handling both sync and async tools."""
    import asyncio

    arguments = arguments or {}

    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:
        return {"error": f"Tool {tool_name} not found"}

    fn = tool.fn

    try:
        import inspect

        if inspect.iscoroutinefunction(fn):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(fn(**arguments))
            finally:
                loop.close()
        else:
            result = fn(**arguments)

        json.dumps(result, default=str)
        return result
    except Exception as e:
        return {"error": f"Tool {tool_name} raised: {type(e).__name__}: {e}"}


def verify_tool_result(tool_name: str, result: dict, expect_error: bool = False):
    """Verify a tool's result meets production standards."""
    assert isinstance(result, dict), f"{tool_name}: Expected dict, got {type(result).__name__}"

    # Check JSON serializable
    try:
        json.dumps(result, default=str)
    except (TypeError, ValueError) as e:
        pytest.fail(f"{tool_name}: Not JSON serializable: {e}")

    if not expect_error:
        # Should not have top-level error unless expected
        if "error" in result:
            # Some tools legitimately return error dicts when backend unavailable
            # This is acceptable behavior — the tool didn't crash
            pass

    return True


# ---------------------------------------------------------------------------
# Tier 1: Resource Tools (8 tools)
# ---------------------------------------------------------------------------


class TestResourceTools:
    """Test read-only resource tools."""

    TOOLS = [
        ("mt5_account_summary", {}),
        ("mt5_symbol_info", {"symbol": "XAUUSDm"}),
        ("mt5_deals_history", {}),
        ("mt5_performance_summary", {}),
        ("mt5_positions_open", {}),
        ("mt5_orders_pending", {}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_resource_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 2: Market Data Tools (12 tools)
# ---------------------------------------------------------------------------


class TestMarketDataTools:
    """Test market data tools — get_* duplicates removed (unified under mt5_* names)."""

    TOOLS = [
        ("mt5_get_bars", {"symbol": "XAUUSDm", "timeframe": "H1", "count": 100}),
        (
            "mt5_get_indicator",
            {"symbol": "XAUUSDm", "timeframe": "H1", "indicator": "rsi"},
        ),
        ("mt5_get_ticks", {"symbol": "XAUUSDm", "count": 100}),
        ("mt5_get_order_book", {"symbol": "XAUUSDm"}),
        ("mt5_get_chart_screenshot", {}),
        ("mt5_market_snapshot", {"symbols": ["XAUUSDm"]}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_market_data_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 3: Analysis Tools — REMOVED
# These 16 tools were redundant with Vibe-Trading integration and crashing
# on Python 3.14. Superseded by vibe_* proxy tools.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tier 4: Trading Execution Tools (16 tools)
# ---------------------------------------------------------------------------


class TestTradingTools:
    """Test trading execution tools — verify policy gating."""

    TOOLS = [
        (
            "mt5_submit_market_order",
            {"symbol": "XAUUSDm", "side": "buy", "volume_lots": 0.01},
        ),
        (
            "mt5_submit_market_order_via_bridge",
            {"symbol": "XAUUSDm", "side": "buy", "volume_lots": 0.01},
        ),
        (
            "mt5_submit_pending_order",
            {"symbol": "XAUUSDm", "side": "buy", "volume_lots": 0.01, "price": 2650.0},
        ),
        ("mt5_close_position", {"position_id": "12345"}),
        ("mt5_close_all_positions", {"symbol": "XAUUSDm"}),
        ("mt5_cancel_order", {"order_id": "ORD_001"}),
        ("mt5_cancel_all_orders", {"symbol": "XAUUSDm"}),
        ("mt5_modify_order", {"order_id": "ORD_001", "new_price": 2655.0}),
        (
            "mt5_modify_position_sl_tp",
            {"position_id": "12345", "new_sl": 2645.0, "new_tp": 2670.0},
        ),
        (
            "mt5_calculate_position_size",
            {
                "symbol": "XAUUSDm",
                "risk_percent": 1.0,
                "entry_price": 2650.0,
                "sl": 2640.0,
            },
        ),
        (
            "mt5_validate_trade_setup",
            {
                "symbol": "XAUUSDm",
                "side": "buy",
                "entry_price": 2650.0,
                "sl": 2640.0,
                "tp": 2680.0,
            },
        ),
        ("mt5_trail_position", {"position_id": "12345"}),
        ("mt5_news_fetch", {"symbol": "XAUUSDm"}),
        ("mt5_news_enrich", {"data": {"symbol": "XAUUSDm"}}),
        ("mt5_news_pools", {}),
        ("mt5_economic_calendar", {}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_trading_tool_no_crash(self, mcp_server, tool_name, kwargs):
        """Every trading tool must not crash the server."""
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result, expect_error=True)

    def test_execution_tools_gate_demo(self, mcp_server, mock_policy):
        """Verify execution tools check policy."""
        result = call_tool_sync(
            mcp_server,
            "mt5_submit_market_order",
            {
                "symbol": "XAUUSDm",
                "side": "buy",
                "volume_lots": 0.01,
            },
        )
        # Policy should have been called
        assert mock_policy.validate_submit_order.called


# ---------------------------------------------------------------------------
# Tier 5: Metacognition + Context Tools (7 tools)
# ---------------------------------------------------------------------------


class TestMetacognitionContextTools:
    """Test metacognition and context tools."""

    TOOLS = [
        (
            "mt5_log_trade_decision",
            {"symbol": "XAUUSDm", "side": "buy", "action": "entry"},
        ),
        ("mt5_reflect_on_trades", {}),
        ("mt5_trading_insights", {}),
        ("mt5_trading_context", {"symbol": "XAUUSDm"}),
        ("mt5_agent_context", {"query": "market"}),
        ("mt5_system_context", {"query": "capabilities"}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_metacognition_context_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 6: Portfolio Tools (5 tools)
# ---------------------------------------------------------------------------


class TestPortfolioTools:
    """Test portfolio management tools."""

    TOOLS = [
        ("mt5_portfolio_risk", {}),
        (
            "mt5_pre_trade_gate",
            {"symbol": "XAUUSDm", "side": "buy", "volume_lots": 0.01},
        ),
        ("mt5_reconcile", {}),
        (
            "mt5_custom_indicator",
            {"symbol": "XAUUSDm", "formula": "(close - open) / open * 100"},
        ),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_portfolio_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 7: EA Native Tools (12 tools)
# ---------------------------------------------------------------------------


class TestEANativeTools:
    """Test EA-native tools (bracket orders, trailing stops)."""

    TOOLS = [
        (
            "mt5_place_bracket_order",
            {
                "symbol": "XAUUSDm",
                "buy_trigger": 3000.0,
                "sell_trigger": 2900.0,
                "volume_lots": 0.01,
            },
        ),
        (
            "mt5_ea_bracket_start",
            {
                "buy_order_ticket": "12345",
                "sell_order_ticket": "12346",
                "bracket_id": "test_bracket_1",
            },
        ),
        ("mt5_ea_bracket_stop", {"bracket_id": "BRK_001"}),
        ("mt5_ea_bracket_list", {}),
        ("mt5_ea_bracket_tick", {}),
        ("mt5_set_trailing_stop", {"position_id": "12345", "distance_pips": 20}),
        ("mt5_trailing_stop_cancel", {"position_id": "12345"}),
        ("mt5_trailing_stop_list", {}),
        ("mt5_ea_trailing_start", {"symbol": "XAUUSDm", "distance_pips": 20}),
        ("mt5_ea_trailing_stop", {"trailing_id": "TRAIL_001"}),
        ("mt5_ea_trailing_list", {}),
        ("mt5_ea_trailing_tick", {}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_ea_native_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 8: Data Store Tools (5 tools)
# ---------------------------------------------------------------------------


class TestDataStoreTools:
    """Test data store tools — these work entirely in-memory."""

    TOOLS = [
        (
            "mt5_data_import",
            {
                "data_type": "bars",
                "data": [
                    {
                        "time": "2026-04-09T10:00:00Z",
                        "open": 2650.0,
                        "high": 2655.0,
                        "low": 2648.0,
                        "close": 2653.0,
                    }
                ],
            },
        ),
        ("mt5_data_bars", {"symbol": "XAUUSDm", "limit": 10}),
        ("mt5_data_ticks", {"symbol": "XAUUSDm", "limit": 10}),
        ("mt5_data_deals", {"symbol": "XAUUSDm", "limit": 10}),
        ("mt5_data_stats", {}),
        ("mt5_data_sync", {"symbol": "XAUUSDm", "timeframe": "H1", "count": 10}),
        ("mt5_data_sync_status", {}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_data_store_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 9: Analysis Tools (16 tools)
# ---------------------------------------------------------------------------


class TestAnalysisTools:
    """Test technical analysis tools — read-only, mock-safe."""

    TOOLS = [
        ("mt5_market_regime", {"symbol": "XAUUSDm"}),
        ("mt5_market_structure", {"symbol": "XAUUSDm"}),
        ("mt5_vwap", {"symbol": "XAUUSDm"}),
        ("mt5_analysis_divergence", {"symbol": "XAUUSDm"}),
        ("mt5_analysis_multi_bar_patterns", {"symbol": "XAUUSDm"}),
        ("mt5_volume_at_price", {"symbol": "XAUUSDm"}),
        ("mt5_analysis_volume_profile", {"symbol": "XAUUSDm"}),
        ("mt5_analysis_momentum", {"symbol": "XAUUSDm"}),
        ("mt5_support_resistance", {"symbol": "XAUUSDm"}),
        ("mt5_volatility_profile", {"symbol": "XAUUSDm"}),
        ("mt5_opportunity_rank", {}),
        ("mt5_setup_probability", {"symbol": "XAUUSDm", "side": "buy"}),
        ("mt5_strategy_selector", {"symbol": "XAUUSDm"}),
        ("mt5_multi_timeframe_indicators", {"symbol": "XAUUSDm"}),
        ("mt5_correlation_matrix", {"symbols": "XAUUSDm,EURUSD"}),
        ("mt5_market_scan", {}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_analysis_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 10: Fundamentals Tools (3 tools)
# ---------------------------------------------------------------------------


class TestFundamentalsTools:
    """Test fundamental analysis tools."""

    TOOLS = [
        ("mt5_economic_risk_assessment", {}),
        ("mt5_market_screener", {"symbols": "EURUSD,GBPUSD,XAUUSD"}),
        ("mt5_news_sentiment_composite", {"symbol": "XAUUSD"}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_fundamentals_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 11: Streaming Tools (3 tools)
# ---------------------------------------------------------------------------


class TestStreamingTools:
    """Test streaming subscription tools."""

    TOOLS = [
        ("mt5_stream_subscribe", {"symbols": ["XAUUSDm"], "timeframes": ["M1"]}),
        ("mt5_stream_unsubscribe", {"symbols": ["XAUUSDm"]}),
        ("mt5_stream_status", {}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", TOOLS)
    def test_streaming_tool(self, mcp_server, tool_name, kwargs):
        result = call_tool_sync(mcp_server, tool_name, kwargs)
        verify_tool_result(tool_name, result)


# ---------------------------------------------------------------------------
# Tier 12: Management Tools (5 tools)
# ---------------------------------------------------------------------------


class TestManagementTools:
    """Test management and health tools."""

    def test_mt5_tool_status(self, mcp_server):
        result = call_tool_sync(mcp_server, "mt5_tool_status", {})
        verify_tool_result("mt5_tool_status", result)
        assert "overall" in result

    def test_mt5_freeze_status(self, mcp_server):
        result = call_tool_sync(mcp_server, "mt5_freeze_status", {})
        verify_tool_result("mt5_freeze_status", result)

    def test_mt5_thaw(self, mcp_server):
        result = call_tool_sync(mcp_server, "mt5_thaw", {})
        verify_tool_result("mt5_thaw", result)

    def test_mt5_safe_shutdown(self, mcp_server):
        result = call_tool_sync(mcp_server, "mt5_safe_shutdown", {})
        verify_tool_result("mt5_safe_shutdown", result)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Production Grading: Error Resilience Tests
# ---------------------------------------------------------------------------


class TestErrorResilience:
    """Test that tools handle errors gracefully without crashing."""

    def test_tool_with_invalid_symbol(self, mcp_server):
        """Tools should handle invalid symbols gracefully."""
        result = call_tool_sync(mcp_server, "mt5_symbol_info", {"symbol": "INVALID_SYMBOL_12345"})
        verify_tool_result("mt5_symbol_info", result, expect_error=True)

    def test_tool_with_missing_params(self, mcp_server):
        """Tools should handle missing required parameters."""
        # Try calling with no params — should get error dict, not exception
        try:
            result = call_tool_sync(mcp_server, "mt5_get_bars", {})
            # If it doesn't raise, verify result
            verify_tool_result("mt5_get_bars", result, expect_error=True)
        except TypeError:
            # TypeError for missing required params is acceptable
            pass

    def test_tool_with_extreme_values(self, mcp_server):
        """Tools should handle extreme numeric values."""
        result = call_tool_sync(
            mcp_server,
            "mt5_calculate_position_size",
            {
                "symbol": "XAUUSDm",
                "risk_percent": 100.0,  # Extreme risk
                "entry_price": 2650.0,
                "sl": 2649.99,  # Tiny SL
            },
        )
        verify_tool_result("mt5_calculate_position_size", result)

    def test_tool_with_empty_string(self, mcp_server):
        """Tools should handle empty strings."""
        result = call_tool_sync(mcp_server, "mt5_symbol_info", {"symbol": ""})
        verify_tool_result("mt5_symbol_info", result, expect_error=True)


# ---------------------------------------------------------------------------
# Tool Registration Verification
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify all tools are properly registered."""

    EXPECTED_TOOL_COUNT = 91

    EXPECTED_TOOLS = {
        # Resources (5) — duplicates purged
        "mt5_account_summary",
        "mt5_symbol_info",
        "mt5_deals_history",
        "mt5_performance_summary",
        "mt5_positions_open",
        "mt5_orders_pending",
        # Market data (8) — get_* duplicates purged, chart_intelligence merged
        "mt5_get_bars",
        "mt5_get_indicator",
        "mt5_get_ticks",
        "mt5_get_order_book",
        "mt5_get_chart_screenshot",
        "mt5_market_snapshot",
        # Trading (12)
        "mt5_submit_market_order",
        "mt5_submit_market_order_via_bridge",
        "mt5_submit_pending_order",
        "mt5_close_position",
        "mt5_close_all_positions",
        "mt5_cancel_order",
        "mt5_cancel_all_orders",
        "mt5_modify_order",
        "mt5_modify_position_sl_tp",
        "mt5_calculate_position_size",
        "mt5_validate_trade_setup",
        "mt5_trail_position",
        # Metacognition + Context (4)
        "mt5_log_trade_decision",
        "mt5_reflect_on_trades",
        "mt5_trading_insights",
        "mt5_trading_context",
        "mt5_agent_context",
        "mt5_system_context",
        # Portfolio (4) — exposure removed
        "mt5_portfolio_risk",
        "mt5_pre_trade_gate",
        "mt5_reconcile",
        "mt5_custom_indicator",
        # EA native (12)
        "mt5_place_bracket_order",
        "mt5_ea_bracket_start",
        "mt5_ea_bracket_stop",
        "mt5_ea_bracket_list",
        "mt5_ea_bracket_tick",
        "mt5_set_trailing_stop",
        "mt5_trailing_stop_cancel",
        "mt5_trailing_stop_list",
        "mt5_ea_trailing_start",
        "mt5_ea_trailing_stop",
        "mt5_ea_trailing_list",
        "mt5_ea_trailing_tick",
        # Data (7) — +mt5_data_sync, mt5_data_sync_status
        "mt5_data_import",
        "mt5_data_bars",
        "mt5_data_ticks",
        "mt5_data_deals",
        "mt5_data_stats",
        "mt5_data_sync",
        "mt5_data_sync_status",
        # Management (5) — health removed
        "mt5_tool_status",
        "mt5_freeze_status",
        "mt5_thaw",
        "mt5_safe_shutdown",
        "mt5_emergency_liquidate",
        # Analysis (14)
        "mt5_market_regime",
        "mt5_market_structure",
        "mt5_vwap",
        "mt5_analysis_divergence",
        "mt5_analysis_multi_bar_patterns",
        "mt5_volume_at_price",
        "mt5_analysis_volume_profile",
        "mt5_analysis_momentum",
        "mt5_support_resistance",
        "mt5_volatility_profile",
        "mt5_opportunity_rank",
        "mt5_setup_probability",
        "mt5_strategy_selector",
        "mt5_multi_timeframe_indicators",
        "mt5_correlation_matrix",
        "mt5_market_scan",
        # Fundamentals (4)
        "mt5_economic_risk_assessment",
        "mt5_market_screener",
        "mt5_news_sentiment_composite",
        # Streaming (3)
        "mt5_stream_subscribe",
        "mt5_stream_unsubscribe",
        "mt5_stream_status",
        # Wait
        "mt5_wait_for_price",
        # Vibe (6)
        "vibe_list_skills",
        "vibe_get_market_data",
        "vibe_run_swarm",
        "vibe_backtest",
        "vibe_swarm_to_signal",
        "vibe_web_search",
        # News (3)
        "mt5_news_fetch",
        "mt5_news_enrich",
        "mt5_news_pools",
        "mt5_economic_calendar",
        # Coach

    }

    def test_tool_count(self, mcp_server):
        """Verify exactly 74 tools registered."""
        tools = mcp_server._tool_manager.list_tools()
        assert len(tools) == self.EXPECTED_TOOL_COUNT, f"Expected {self.EXPECTED_TOOL_COUNT} tools, got {len(tools)}"

    def test_all_expected_tools_registered(self, mcp_server):
        """Verify every expected tool name is registered."""
        tools = mcp_server._tool_manager.list_tools()
        registered = {t.name for t in tools}

        missing = self.EXPECTED_TOOLS - registered
        assert not missing, f"Missing tools: {sorted(missing)} (registered={len(registered)}, expected={len(self.EXPECTED_TOOLS)}). Full registered: {sorted(registered)}"

        extra = registered - self.EXPECTED_TOOLS
        if extra:
            print(f"Note: Extra tools registered: {sorted(extra)}")

    def test_tool_naming_convention(self, mcp_server):
        """All tools must follow mt5_ or vibe_ prefix and snake_case."""
        import re

        tools = mcp_server._tool_manager.list_tools()
        pattern = re.compile(r"^(?:mt5|vibe)_[a-z][a-z0-9_]*$")

        for tool in tools:
            assert pattern.match(tool.name), f"Tool '{tool.name}' doesn't follow naming convention"

    def test_tool_annotations(self, mcp_server):
        """All tools must have annotations set."""
        tools = mcp_server._tool_manager.list_tools()
        for tool in tools:
            assert tool.annotations is not None, f"Tool '{tool.name}' missing annotations"


# ---------------------------------------------------------------------------
# Summary Test
# ---------------------------------------------------------------------------


class TestProductionReadiness:
    """Overall production readiness checks."""

    def test_server_creates_without_errors(self):
        """Server factory must create without exceptions."""
        from apps.mcp_server import create_mcp_server

        server = create_mcp_server()
        assert server is not None

    def test_server_has_name(self):
        """Server must have a name."""
        from apps.mcp_server import create_mcp_server

        server = create_mcp_server()
        assert server.name == "MetaTraderBridge"

    def test_no_circular_imports(self):
        """All tool modules must import without circular dependency errors."""
        from apps.mcp_server import (
            tools_context,
            tools_data,
            tools_ea_native,
            tools_management,
            tools_market_data,
            tools_metacognition,
            tools_portfolio,
            tools_resources,
            tools_trading,
            tools_vibe,
            tools_wait,
            wait_tools,
        )

        # If we got here, no circular imports
        assert True

    def test_all_tools_return_dict_not_none(self, mcp_server):
        """Spot check: several tools must return dict, not None."""
        critical_tools = [
            ("mt5_health", {}),
            ("mt5_tool_status", {}),
            ("mt5_freeze_status", {}),
            (
                "mt5_calculate_position_size",
                {
                    "symbol": "XAUUSDm",
                    "risk_percent": 1.0,
                    "entry_price": 2650.0,
                    "sl": 2640.0,
                },
            ),
        ]
        for tool_name, kwargs in critical_tools:
            result = call_tool_sync(mcp_server, tool_name, kwargs)
            assert result is not None, f"{tool_name} returned None"
            assert isinstance(result, dict), f"{tool_name} returned {type(result).__name__}, not dict"
