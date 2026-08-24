"""Shared infrastructure for the MetaTraderBridge FastMCP server.

Provides lazy-loaded singletons (TCP client, gateway, HTTP client, settings),
bridge communication helpers, payload parsers, and freeze state management.
All tool modules import from here — no direct imports from main.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx

from mt5_mcp.observability.logging import logger
from mt5_mcp.settings.config import get_settings


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_http_client: httpx.Client | None = None
_gw: Any | None = None
_settings: Any | None = None

_TCP_BRIDGE_ENABLED = os.getenv("MT5_TCP_BRIDGE_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# In-process TCP Bridge Server (embedded mode)
# When set, _tcp_send_and_await calls enqueue_and_await() directly
# instead of going through TCPBridgeClient on port 8026.
# Set by apps/bridge/main.py before starting uvicorn.
# ---------------------------------------------------------------------------

_bridge_server: Any | None = None
_bridge_loop: asyncio.AbstractEventLoop | None = None


def set_bridge_server(
    server: Any, loop: asyncio.AbstractEventLoop | None = None
) -> None:
    global _bridge_server, _bridge_loop
    _bridge_server = server
    _bridge_loop = loop or asyncio.get_event_loop()
    if server is not None:
        logger.info(
            f"TCP bridge server wired for in-process calls "
            f"(EA connected: {getattr(server, 'ea_connected', False)})"
        )


def is_bridge_connected() -> bool:
    """Check if the in-process bridge server has an active EA connection."""
    if _bridge_server is None:
        return False
    return bool(getattr(_bridge_server, "ea_connected", False))


# ---------------------------------------------------------------------------
# Shared indicator defaults — single source of truth.
# All tool modules should import this instead of maintaining local copies.
# ---------------------------------------------------------------------------

INDICATOR_DEFAULTS: dict[str, dict[str, int]] = {
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "bbands": {"period": 20, "deviation": 2},
    "stoch": {"k_period": 5, "d_period": 3, "slowing": 3},
    "atr": {"period": 14},
    "adx": {"period": 14},
    "dmi": {"period": 14},
    "ichimoku": {"tenkan": 9, "kijun": 26, "senkou": 52},
    "cci": {"period": 14},
    "rsi": {"period": 14},
    "sma": {"period": 20},
    "ema": {"period": 20},
    "wma": {"period": 20},
    "momentum": {"period": 14},
    "williams": {"period": 14},
}


def get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=10.0,
            http2=False,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            ),
        )
    return _http_client


def get_gateway():
    global _gw
    if _gw is None:
        from mt5_mcp.services.execution_gateway.service import ExecutionGateway

        _gw = ExecutionGateway()
    return _gw


def get_settings_cached():
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


# ---------------------------------------------------------------------------
# TCP bridge: persistent client on dedicated background thread
# ---------------------------------------------------------------------------

_tcp_client: Any | None = None
_tcp_loop: asyncio.AbstractEventLoop | None = None
_tcp_thread: threading.Thread | None = None
_tcp_ready = threading.Event()
_tcp_lock = threading.RLock()


def _run_tcp_event_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    global _tcp_loop
    _tcp_loop = loop
    _tcp_ready.set()
    try:
        loop.run_forever()
    finally:
        loop.close()


def _get_tcp_loop() -> asyncio.AbstractEventLoop | None:
    global _tcp_loop, _tcp_thread
    if _tcp_loop is not None and _tcp_loop.is_running():
        return _tcp_loop
    with _tcp_lock:
        if _tcp_loop is not None and _tcp_loop.is_running():
            return _tcp_loop
        _tcp_ready.clear()
        _tcp_thread = threading.Thread(target=_run_tcp_event_loop, daemon=True)
        _tcp_thread.start()
        _tcp_ready.wait(timeout=5.0)
        return _tcp_loop


def _get_tcp_client() -> Any | None:
    global _tcp_client
    if _tcp_client is not None:
        return _tcp_client

    with _tcp_lock:
        if _tcp_client is not None:
            return _tcp_client

        loop = _get_tcp_loop()
        if loop is None:
            logger.warning("Could not start TCP event loop")
            return None

        try:
            from mt5_mcp.services.tcp_bridge_client import TCPBridgeClient

            _tcp_client = TCPBridgeClient()
            future = asyncio.run_coroutine_threadsafe(_tcp_client.connect(), loop)
            future.result(timeout=10.0)
            logger.info("TCP bridge client connected (shared, persistent)")
        except Exception as e:
            logger.warning(f"TCP bridge client connect failed: {e}")
            _tcp_client = None
    return _tcp_client


# ---------------------------------------------------------------------------
# Bridge communication helpers
# ---------------------------------------------------------------------------


def _await_result(req_id: str, timeout_s: float = 20.0, poll_s: float = 0.1) -> dict:
    loop = _get_tcp_loop()
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            _await_result_async(req_id, timeout_s, poll_s), loop
        )
        try:
            return future.result(timeout=timeout_s + 10.0)
        except TimeoutError as e:
            logger.warning(
                f"_await_result({req_id}): async timeout after {timeout_s}s — {e}"
            )
            return {"status": "timeout", "error": "timeout"}
        except Exception as e:
            logger.warning(f"_await_result({req_id}): async failed — {e}")
            return {"status": "timeout", "error": "timeout"}
    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    end = time.time() + timeout_s
    try:
        while time.time() < end:
            r = client.get(f"{gw_url}/bridge/results/{req_id}")
            if r.status_code == 200:
                data = r.json()
                if data.get("status") in {"completed", "error"}:
                    return data
            time.sleep(poll_s)
    except Exception as e:
        logger.warning(f"_await_result({req_id}): HTTP poll failed — {e}")
    return {"status": "timeout", "error": "timeout"}


async def _await_result_async(req_id: str, timeout_s: float, poll_s: float) -> dict:
    gw_url = get_settings_cached().gateway_url
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{gw_url}/bridge/results/{req_id}")
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") in {"completed", "error"}:
                        return data
        except Exception:
            pass
        await asyncio.sleep(poll_s)
    return {"status": "timeout", "error": "timeout"}


def _tcp_send_and_await(
    type: str, payload: dict[str, Any], timeout_s: float = 20.0
) -> dict[str, Any] | None:
    if not _TCP_BRIDGE_ENABLED:
        return None

    # Fast path: in-process bridge server (no network hop, no event loop creation)
    if _bridge_server is not None and _bridge_loop is not None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                _bridge_server.enqueue_and_await(type, payload, timeout=timeout_s),
                _bridge_loop,
            )
            result = future.result(timeout=timeout_s + 5.0)
            inner = result.get("payload", result)
            return {"status": "completed", "result": {"payload": inner}}
        except ConnectionError:
            logger.debug(f"_tcp_send_and_await({type}): EA not connected via bridge")
            return None
        except TimeoutError as e:
            logger.warning(
                f"_tcp_send_and_await({type}): bridge timeout after {timeout_s}s — {e}"
            )
            return None
        except Exception as e:
            logger.warning(f"_tcp_send_and_await({type}): bridge failed — {e}")
            return None

    # Legacy path: TCPBridgeClient on background thread (separate process mode)
    try:
        client = _get_tcp_client()
        if client is None:
            logger.debug(f"_tcp_send_and_await({type}): TCP client unavailable")
            return None
        loop = _get_tcp_loop()
        if loop is None:
            logger.debug(f"_tcp_send_and_await({type}): TCP event loop unavailable")
            return None
        future = asyncio.run_coroutine_threadsafe(
            client.send_command(type, payload, timeout=timeout_s), loop
        )
        result = future.result(timeout=timeout_s + 5.0)
        inner = result.get("payload", result)
        return {"status": "completed", "result": {"payload": inner}}
    except TimeoutError as e:
        logger.warning(f"_tcp_send_and_await({type}): timeout after {timeout_s}s — {e}")
    except Exception as e:
        logger.warning(f"_tcp_send_and_await({type}): failed — {e}")
    return None


def _batch_enqueue_and_await(
    commands: list[dict[str, Any]], timeout_s: float = 20.0
) -> list[dict]:
    # Fast path: in-process bridge server (no network hop, no event loop creation)
    if _bridge_server is not None and _bridge_loop is not None:

        async def _run_all_bridge():
            results = [None] * len(commands)
            tasks = []
            for i, cmd in enumerate(commands):
                task = asyncio.create_task(
                    _bridge_server.enqueue_and_await(
                        type=cmd["type"],
                        payload={k: v for k, v in cmd.items() if k != "type"},
                        timeout=timeout_s,
                    )
                )
                tasks.append((i, task))
            for i, task in tasks:
                try:
                    r = await task
                    results[i] = {
                        "status": "completed",
                        "result": {"payload": r.get("payload", r)},
                    }
                except ConnectionError:
                    results[i] = None
                except TimeoutError:
                    results[i] = {"status": "timeout", "error": "timeout"}
                except Exception as e:
                    results[i] = {"status": "error", "error": str(e)}
            return results

        try:
            future = asyncio.run_coroutine_threadsafe(
                _run_all_bridge(), _bridge_loop
            )
            results = future.result(timeout=timeout_s + 10.0)
            if all(r is not None for r in results):
                return results
        except Exception:
            pass

    tcp_ok = False
    if _TCP_BRIDGE_ENABLED:
        try:
            client = _get_tcp_client()
            if client is not None:
                loop = _get_tcp_loop()
                if loop is not None:

                    async def _run_all():
                        results = [None] * len(commands)
                        tasks = []
                        for i, cmd in enumerate(commands):
                            task = asyncio.create_task(
                                client.send_command(
                                    type=cmd["type"],
                                    payload={
                                        k: v for k, v in cmd.items() if k != "type"
                                    },
                                    timeout=timeout_s,
                                )
                            )
                            tasks.append((i, task))
                        for i, task in tasks:
                            try:
                                results[i] = await task
                            except Exception:
                                results[i] = None
                        return results

                    future = asyncio.run_coroutine_threadsafe(_run_all(), loop)
                    results = future.result(timeout=timeout_s + 10.0)
                    tcp_ok = True

                    if all(r is not None for r in results):
                        return [
                            {
                                "status": "completed",
                                "result": {"payload": r.get("payload", r)},
                            }
                            for r in results
                        ]
        except Exception:
            pass

    gw_url = get_settings_cached().gateway_url
    client = get_http_client()
    r = client.post(
        f"{gw_url}/bridge/commands/batch",
        json={"commands": commands},
        timeout=timeout_s,
    )
    r.raise_for_status()
    batch = r.json()
    req_ids = batch.get("ids", [])
    results = []
    for rid in req_ids:
        results.append(_await_result(rid, timeout_s=timeout_s))
    return results


# ---------------------------------------------------------------------------
# Payload parsers
# ---------------------------------------------------------------------------


def _parse_payload(payload) -> dict:
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return {}
    elif isinstance(payload, dict):
        return payload
    return {}


def _parse_payload_dict(result: dict) -> dict:
    if not result or result.get("status") != "completed":
        return {}
    payload = result.get("result", {}).get("payload", {})
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return {}
    elif isinstance(payload, dict):
        return payload
    return {}


# Mapping of indicator name → primary value key in EA JSON responses.
# The EA returns different key names depending on indicator type.
_INDICATOR_VALUE_KEYS: dict[str, str] = {
    "rsi": "value",
    "atr": "value",
    "sma": "value",
    "ema": "value",
    "wma": "value",
    "smma": "value",
    "cci": "value",
    "obv": "value",
    "adx": "adx",
    "dmi": "adx",
    "macd": "main",
    "stoch": "k_val",
    "bbands": "upper",  # representative; full result has upper/middle/lower
    "ichimoku": "tenkan",  # representative; full result has 5 lines
}


def _parse_indicator_value(result: dict) -> float | None:
    """Extract the primary numeric value from an indicator EA response.

    Handles the variety of key names the EA uses:
      - RSI/ATR/SMA/EMA/WMA/SMMA/CCI/OBV → "value"
      - ADX/DMI → "adx"
      - MACD → "main"
      - Stochastic → "k_val"
      - Bollinger Bands → "upper"
      - Ichimoku → "tenkan"

    Returns None (not 0.0) when the value is unavailable, so callers
    can distinguish "zero" from "missing".
    """
    if not result or result.get("status") != "completed":
        return None
    payload = result.get("result", {}).get("payload", {})
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:
            return None
    elif isinstance(payload, dict):
        data = payload
    else:
        return None

    if not data:
        return None

    # Detect indicator type from the EA response
    indicator_name = (data.get("indicator") or "").lower()
    primary_key = _INDICATOR_VALUE_KEYS.get(indicator_name, "value")

    v = data.get(primary_key)
    if v is not None:
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    # Fallback: try "value" key for any indicator
    v = data.get("value")
    if v is not None:
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    return None


def _parse_indicator_value_from_data(data: dict, indicator_name: str) -> float | None:
    """Extract the primary numeric value from already-parsed indicator data.

    Prefers the indicator name from the EA response data itself (more reliable
    than the caller-provided name, which may use different aliases).
    """
    if not data:
        return None

    ea_indicator = (data.get("indicator") or indicator_name or "").lower()
    primary_key = _INDICATOR_VALUE_KEYS.get(ea_indicator, "value")

    v = data.get(primary_key)
    if v is not None:
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    # Fallback: try "value" key for any indicator
    v = data.get("value")
    if v is not None:
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    return None


def _first_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid = bids[0].get("price") if bids and isinstance(bids[0], dict) else None
    ask = asks[0].get("price") if asks and isinstance(asks[0], dict) else None
    if bid is None:
        bid = book.get("bid")
    if ask is None:
        ask = book.get("ask")
    return (
        float(bid) if bid is not None else None,
        float(ask) if ask is not None else None,
    )


# ---------------------------------------------------------------------------
# Freeze state management
# ---------------------------------------------------------------------------

_shutdown_state = {"frozen": False, "frozen_at": None, "frozen_by": None}


def is_frozen() -> bool:
    return _shutdown_state["frozen"]


def set_frozen(frozen: bool, by: str | None = None):
    _shutdown_state["frozen"] = frozen
    _shutdown_state["frozen_at"] = (
        datetime.now(timezone.utc).isoformat() if frozen else None
    )
    _shutdown_state["frozen_by"] = by


def thaw():
    set_frozen(False)


def _check_frozen_response() -> dict | None:
    if is_frozen():
        return {
            "error": "Trading is frozen",
            "frozen_at": _shutdown_state["frozen_at"],
            "frozen_by": _shutdown_state["frozen_by"],
        }
    return None
