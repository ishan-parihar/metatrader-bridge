"""MetaTraderBridge MCP tool wrappers for Vibe-Trading capabilities.

These functions are registered with the MetaTraderBridge MCP server
and forward calls to the Vibe-Trading subprocess via the gateway client.
"""

from __future__ import annotations

import json
from typing import Optional

from .client import VibeBridgeClient
from .signal_translator import extract_signal_from_swarm_report

_client: Optional[VibeBridgeClient] = None


def _get_client() -> VibeBridgeClient:
    """Get or create the singleton VibeBridgeClient."""
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
        return json.dumps(
            {
                "status": "no_signal",
                "message": "No actionable trade signal found in report",
            }
        )
    return json.dumps(
        {
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
        }
    )


async def vibe_web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Vibe-Trading for market news and sentiment."""
    client = _get_client()
    await client.ensure_ready()
    return await client.web_search(query, max_results)
