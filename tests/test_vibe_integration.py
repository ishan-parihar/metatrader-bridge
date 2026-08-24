"""End-to-end integration tests for Vibe-Trading <-> MetaTraderBridge signal pipeline.

Tests the full signal flow: swarm report -> signal extraction -> order params,
backtest results -> viability signal, tool wrappers, gateway routes, and
symbol mapping edge cases.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.vibe_bridge.client import VibeBridgeClient, VIBE_TOOLS
from apps.vibe_bridge.signal_translator import (
    SignalAction,
    SignalStrength,
    TradeSignal,
    extract_signal_from_backtest,
    extract_signal_from_swarm_report,
)
from apps.vibe_bridge.tools import vibe_swarm_to_signal


class TestSignalPipeline:
    """Test the full pipeline: swarm report -> signal extraction -> order params."""

    def test_full_pipeline_buy_signal(self):
        """Extract BUY signal from swarm report and convert to order params."""
        report = """
        The Investment Committee has reached a decision:
        BUY XAU/USD at $2350.00
        Entry at 2350.00, stop loss at 2340.00
        Take profit at 2380.00
        Confidence: 75%

        Rationale: Strong bullish momentum driven by Fed policy uncertainty.
        """
        signal = extract_signal_from_swarm_report(report, "investment_committee")
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.symbol == "XAUUSD"

        order_params = signal.to_order_params()
        assert order_params["symbol"] == "XAUUSD"
        assert order_params["price"] == 2350.0
        assert order_params["sl"] == 2340.0
        assert order_params["tp"] == 2380.0
        assert "comment" in order_params
        assert "Vibe:" in str(order_params["comment"])

    def test_full_pipeline_sell_signal(self):
        """Extract SELL signal and verify all order fields."""
        report = """
        SELL EUR/USD at 1.0850
        Stop loss at 1.0900, target 1.0750
        Confidence: 85%
        """
        signal = extract_signal_from_swarm_report(report)
        assert signal is not None
        assert signal.action == SignalAction.SELL
        assert signal.confidence == 0.85
        assert signal.strength == SignalStrength.STRONG

        order_params = signal.to_order_params()
        assert order_params["symbol"] == "EURUSD"
        assert order_params["action"] == "SELL"

    def test_full_pipeline_no_signal(self):
        """Report with no actionable signal returns None."""
        report = "Market is consolidating. No clear direction for EUR/USD or XAU/USD."
        signal = extract_signal_from_swarm_report(report)
        assert signal is None

    def test_full_pipeline_backtest_to_order(self):
        """Backtest with good metrics produces BUY signal."""
        result = json.dumps(
            {
                "sharpe_ratio": 1.5,
                "max_drawdown": 0.12,
                "win_rate": 0.62,
                "total_return": 0.25,
            }
        )
        signal = extract_signal_from_backtest(result, "XAU/USD")
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.confidence > 0.3

        order_params = signal.to_order_params()
        assert order_params["symbol"] == "XAUUSD"
        assert order_params["action"] == "BUY"

    def test_backtest_hold_signal(self):
        """Backtest with poor metrics produces HOLD signal."""
        result = json.dumps(
            {
                "sharpe_ratio": 0.3,
                "max_drawdown": 0.25,
                "win_rate": 0.40,
                "total_return": -0.05,
            }
        )
        signal = extract_signal_from_backtest(result, "EUR/USD")
        assert signal is not None
        assert signal.action == SignalAction.HOLD
        assert signal.confidence == 0.2

    def test_backtest_invalid_json_returns_none(self):
        """Malformed JSON returns None."""
        signal = extract_signal_from_backtest("not json at all", "XAUUSD")
        assert signal is None

    def test_swarm_to_signal_tool_sell(self):
        """Test vibe_swarm_to_signal with a SELL signal."""

        async def _run():
            report = "SELL EUR/USD at 1.0850. Stop loss at 1.0900. Target 1.0750."
            result = await vibe_swarm_to_signal(report, "test")
            return json.loads(result)

        data = asyncio.run(_run())
        assert data["status"] == "signal_extracted"
        assert data["action"] == "SELL"
        assert "order_params" in data
        assert data["order_params"]["action"] == "SELL"

    def test_swarm_to_signal_tool_no_signal(self):
        """Test vibe_swarm_to_signal when no signal is found."""

        async def _run():
            report = "Market is consolidating, no clear direction."
            result = await vibe_swarm_to_signal(report)
            return json.loads(result)

        data = asyncio.run(_run())
        assert data["status"] == "no_signal"
        assert "No actionable trade signal" in data["message"]

    def test_swarm_to_signal_tool_buy(self):
        """Test vibe_swarm_to_signal with a BUY signal includes all fields."""

        async def _run():
            report = """
            BUY XAU/USD at 2400. Entry at 2400, stop loss at 2390.
            Take profit at 2430. Confidence: 70%.
            Rationale: Gold rallying on geopolitical tensions.
            """
            result = await vibe_swarm_to_signal(report, "committee")
            return json.loads(result)

        data = asyncio.run(_run())
        assert data["status"] == "signal_extracted"
        assert data["action"] == "BUY"
        assert data["symbol"] == "XAUUSD"
        assert data["entry_price"] == 2400.0
        assert data["stop_loss"] == 2390.0
        assert data["take_profit"] == 2430.0
        assert data["order_params"]["price"] == 2400.0


class TestGatewayRoutesDirect:
    """Test gateway route handler logic with mocked client."""

    @pytest.mark.asyncio
    async def test_vibe_status_when_not_running(self):
        import apps.vibe_bridge.gateway_routes as gw

        gw._client = None
        with patch.object(gw, "get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.get_status.return_value = {"running": False, "pid": None}
            mock_get.return_value = mock_client
            result = await gw.vibe_status()
            assert result["running"] is False

        gw._client = None

    @pytest.mark.asyncio
    async def test_vibe_status_when_running(self):
        import apps.vibe_bridge.gateway_routes as gw

        gw._client = None
        with patch.object(gw, "get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.get_status.return_value = {
                "running": True,
                "pid": 12345,
                "port": 8900,
                "sse_url": "http://127.0.0.1:8900",
            }
            mock_get.return_value = mock_client
            result = await gw.vibe_status()
            assert result["running"] is True
            assert result["pid"] == 12345
            assert result["port"] == 8900

        gw._client = None


class TestClientToolValidation:
    """Test that VibeBridgeClient validates tool names and lifecycle state."""

    def test_all_tools_are_valid_strings(self):
        """Every tool name is a non-empty string without spaces."""
        for tool in VIBE_TOOLS:
            assert isinstance(tool, str)
            assert len(tool) > 0
            assert " " not in tool

    def test_vibe_tools_contains_expected(self):
        """VIBE_TOOLS includes all core expected tools."""
        expected = [
            "list_skills",
            "backtest",
            "run_swarm",
            "get_market_data",
            "web_search",
        ]
        for tool in expected:
            assert tool in VIBE_TOOLS, f"Missing expected tool: {tool}"

    def test_vibe_tools_count(self):
        """VIBE_TOOLS has exactly 17 tools."""
        assert len(VIBE_TOOLS) == 17

    @pytest.mark.asyncio
    async def test_call_invalid_tool_raises(self):
        """Calling an unknown tool raises ValueError."""
        mock_lifecycle = MagicMock()
        mock_lifecycle.is_running = True
        client = VibeBridgeClient(lifecycle=mock_lifecycle)
        with pytest.raises(ValueError, match="Unknown Vibe-Trading tool"):
            await client.call_tool("nonexistent_tool")

    @pytest.mark.asyncio
    async def test_call_tool_when_not_running_raises(self):
        """Calling a tool when server is not running raises RuntimeError."""
        mock_lifecycle = MagicMock()
        mock_lifecycle.is_running = False
        client = VibeBridgeClient(lifecycle=mock_lifecycle)
        with pytest.raises(RuntimeError, match="not running"):
            await client.call_tool("list_skills")


class TestSymbolMappingEdgeCases:
    """Test edge cases in symbol translation via TradeSignal._map_symbol."""

    def test_empty_symbol_passthrough(self):
        """Empty string passes through unchanged."""
        signal = TradeSignal(action=SignalAction.BUY, symbol="")
        params = signal.to_order_params()
        assert params["symbol"] == ""

    def test_lowercase_symbol_uppercased(self):
        """Lowercase symbols are uppercased and slashes removed."""
        signal = TradeSignal(action=SignalAction.BUY, symbol="eur/usd")
        params = signal.to_order_params()
        assert params["symbol"] == "EURUSD"

    def test_gold_variants(self):
        """All common gold symbol formats map to XAUUSD."""
        for sym in ["GOLD", "XAU/USD", "XAUUSDm"]:
            signal = TradeSignal(action=SignalAction.BUY, symbol=sym)
            params = signal.to_order_params()
            assert params["symbol"] == "XAUUSD", f"Failed for {sym}"

    def test_crypto_variants(self):
        """Crypto symbol formats map to MT5-style symbols."""
        signal_btc = TradeSignal(action=SignalAction.BUY, symbol="BTC-USDT")
        assert signal_btc.to_order_params()["symbol"] == "BTCUSD"

        signal_eth = TradeSignal(action=SignalAction.SELL, symbol="ETH-USDT")
        assert signal_eth.to_order_params()["symbol"] == "ETHUSD"

    def test_fx_pairs(self):
        """Forex pair formats map correctly."""
        pairs = [
            ("EUR/USD", "EURUSD"),
            ("GBP/USD", "GBPUSD"),
            ("USD/JPY", "USDJPY"),
        ]
        for raw, expected in pairs:
            signal = TradeSignal(action=SignalAction.BUY, symbol=raw)
            assert signal.to_order_params()["symbol"] == expected, f"Failed for {raw}"

    def test_already_mt5_symbol_unchanged(self):
        """Symbols already in MT5 format are just uppercased."""
        signal = TradeSignal(action=SignalAction.BUY, symbol="XAUUSD")
        params = signal.to_order_params()
        assert params["symbol"] == "XAUUSD"

    def test_risk_reward_auto_calculated(self):
        """Risk/reward ratio is auto-calculated when all prices present."""
        signal = TradeSignal(
            action=SignalAction.BUY,
            symbol="XAUUSD",
            entry_price=2350.0,
            stop_loss=2340.0,
            take_profit=2380.0,
        )
        assert signal.risk_reward == 3.0  # (2380-2350) / (2350-2340) = 30/10

    def test_risk_reward_none_when_missing_prices(self):
        """Risk/reward is None when not all prices are available."""
        signal = TradeSignal(
            action=SignalAction.BUY,
            symbol="XAUUSD",
            entry_price=2350.0,
            stop_loss=2340.0,
            # no take_profit
        )
        assert signal.risk_reward is None

    def test_order_params_includes_timeframe(self):
        """Order params include default H1 timeframe."""
        signal = TradeSignal(action=SignalAction.BUY, symbol="XAUUSD")
        params = signal.to_order_params()
        assert params["timeframe"] == "H1"

    def test_order_params_no_optional_fields_when_none(self):
        """Order params omit optional fields when not set."""
        signal = TradeSignal(action=SignalAction.BUY, symbol="XAUUSD")
        params = signal.to_order_params()
        assert "price" not in params
        assert "sl" not in params
        assert "tp" not in params
        assert "comment" not in params
