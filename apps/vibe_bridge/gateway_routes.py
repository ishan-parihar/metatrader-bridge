"""FastAPI routes exposing Vibe-Trading capabilities through MetaTraderBridge gateway."""

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .client import VibeBridgeClient
from .signal_translator import (
    extract_signal_from_backtest,
    extract_signal_from_swarm_report,
)

router = APIRouter(prefix="/vibe", tags=["vibe-trading"])

# Singleton client
_client: Optional[VibeBridgeClient] = None


def get_client() -> VibeBridgeClient:
    """Get or create VibeBridgeClient singleton."""
    global _client
    if _client is None:
        _client = VibeBridgeClient()
    return _client


# --- Request/Response models ---


class VibeToolRequest(BaseModel):
    tool: str = Field(..., description="Vibe-Trading tool name")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")


class VibeMarketDataRequest(BaseModel):
    codes: list[str] = Field(..., description="Symbols, e.g. ['BTC-USDT', 'AAPL.US']")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    source: str = Field(default="auto")
    interval: str = Field(default="1D")


class VibeSwarmRunRequest(BaseModel):
    preset: str = Field(..., description="Swarm preset name, e.g. 'investment_committee'")
    variables: dict[str, str] = Field(..., description="Preset variables")


class VibeBacktestRequest(BaseModel):
    run_dir: str = Field(..., description="Path to backtest run directory")
    auto_execute: bool = Field(
        default=False,
        description="If true and backtest is good, generate MetaTraderBridge orders",
    )
    symbol: str = Field(default="", description="Symbol for order mapping")


# --- Routes ---


@router.get("/status")
async def vibe_status():
    """Get Vibe-Trading subprocess status."""
    client = get_client()
    return client.get_status()


@router.post("/start")
async def vibe_start():
    """Start Vibe-Trading MCP server."""
    client = get_client()
    started = await client.ensure_ready()
    if not started:
        raise HTTPException(status_code=503, detail="Failed to start Vibe-Trading MCP server")
    return {"status": "running", "url": client.base_url}


@router.post("/stop")
async def vibe_stop():
    """Stop Vibe-Trading MCP server."""
    client = get_client()
    await client._lifecycle.stop()
    return {"status": "stopped"}


@router.post("/tool")
async def vibe_call_tool(req: VibeToolRequest):
    """Call any Vibe-Trading MCP tool.

    Available tools: list_skills, load_skill, backtest, factor_analysis,
    analyze_options, pattern_recognition, get_market_data, web_search,
    read_url, read_document, read_file, write_file, list_swarm_presets,
    run_swarm, get_swarm_status, get_run_result, list_runs.
    """
    client = get_client()
    await client.ensure_ready()
    result = await client.call_tool(req.tool, req.arguments)
    return {"tool": req.tool, "result": result}


@router.get("/skills")
async def vibe_list_skills():
    """List all 69 Vibe-Trading finance skills."""
    try:
        client = get_client()
        await client.ensure_ready()
        result = await client.list_skills()
        return {"skills": result}
    except RuntimeError as e:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail=f"Vibe-Trading server unavailable: {e}. Ensure VIBE_TRADING_DIR is configured and the server is installed.",
        )
    except Exception as e:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail=f"Vibe-Trading error: {e}",
        )


@router.post("/market-data")
async def vibe_market_data(req: VibeMarketDataRequest):
    """Fetch market data via Vibe-Trading (multi-source: crypto, equities, futures)."""
    client = get_client()
    await client.ensure_ready()
    result = await client.get_market_data(
        codes=req.codes,
        start_date=req.start_date,
        end_date=req.end_date,
        source=req.source,
        interval=req.interval,
    )
    return {"data": result}


@router.post("/swarm/run")
async def vibe_swarm_run(req: VibeSwarmRunRequest):
    """Run a Vibe-Trading swarm team (long-running, may take minutes)."""
    client = get_client()
    await client.ensure_ready()
    result = await client.run_swarm(req.preset, req.variables)
    return {"result": result}


@router.get("/swarm/presets")
async def vibe_swarm_presets():
    """List available swarm team presets."""
    client = get_client()
    await client.ensure_ready()
    result = await client.call_tool("list_swarm_presets")
    return {"presets": result}


@router.post("/backtest")
async def vibe_backtest(req: VibeBacktestRequest):
    """Run a backtest and optionally generate MetaTraderBridge orders from results."""
    client = get_client()
    await client.ensure_ready()
    result = await client.backtest(req.run_dir)

    response: dict[str, Any] = {"backtest_result": result}

    if req.auto_execute and req.symbol:
        signal = extract_signal_from_backtest(result, req.symbol)
        if signal and signal.action.value in ("BUY", "SELL"):
            response["signal"] = {
                "action": signal.action.value,
                "symbol": signal.symbol,
                "confidence": signal.confidence,
                "reasoning": signal.reasoning,
                "order_params": signal.to_order_params(),
            }
        else:
            response["signal"] = {
                "action": "HOLD",
                "reason": "Backtest not compelling enough for live execution",
            }

    return response


@router.post("/swarm/analyze-and-trade")
async def vibe_swarm_analyze_and_trade(req: VibeSwarmRunRequest):
    """Run swarm analysis and extract tradeable signals.

    This is the main integration endpoint: runs a swarm team,
    extracts trade signals from the report, and returns
    MetaTraderBridge-compatible order parameters.
    """
    client = get_client()
    await client.ensure_ready()

    # Step 1: Run swarm
    result_str = await client.run_swarm(req.preset, req.variables)

    try:
        result = json.loads(result_str)
    except json.JSONDecodeError:
        result = {"raw": result_str}

    report = result.get("final_report", result_str)

    # Step 2: Extract signal
    signal = extract_signal_from_swarm_report(report, req.preset)

    if signal:
        return {
            "swarm_run_id": result.get("run_id"),
            "signal": {
                "action": signal.action.value,
                "symbol": signal.symbol,
                "strength": signal.strength.value,
                "confidence": signal.confidence,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "risk_reward": signal.risk_reward,
                "order_params": signal.to_order_params(),
            },
            "report_summary": report[:500] if isinstance(report, str) else "",
        }
    else:
        return {
            "swarm_run_id": result.get("run_id"),
            "signal": None,
            "message": "No actionable trade signal found in swarm report",
            "report_summary": report[:500] if isinstance(report, str) else "",
        }
