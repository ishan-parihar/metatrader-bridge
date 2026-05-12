"""StreamManager — polls EA for ticks and broadcasts to subscribed WebSocket clients.

Design:
  - Polls get_ticks every ~1.5s per subscribed symbol via the in-process TCP bridge.
  - Broadcasts tick data to all connected WebSocket clients for that symbol.
  - Thread-safe subscription dict access via asyncio.Lock.
  - Gracefully handles client disconnects (removes dead sockets on send failure).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import WebSocket

from mt5_mcp.observability.logging import logger

# Reuse the shared TCP bridge helper from the MCP server.
# In the combined deployment, _bridge_server is set by apps/bridge/main.py,
# giving us a direct in-process path to the EA via enqueue_and_await.
from apps.mcp_server.shared import _tcp_send_and_await


class StreamManager:
    """Manages WebSocket subscriptions and a background tick-polling loop.

    Supports two subscription modes:
      1. **Client subscriptions** — WebSocket connections registered via
         ``subscribe(ws, symbol)`` from the ``/ws/prices`` endpoint.
         Polling is active only while clients are connected.
      2. **Symbol subscriptions** — symbol names registered via
         ``subscribe_symbol(symbol)`` from MCP tools. These keep polling
         active even without connected WebSocket clients.

    Thread safety:
      All access to protected state is guarded by ``_lock`` (asyncio.Lock).
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, set[WebSocket]] = {}
        self._symbol_subscriptions: set[str] = set()
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._running = False

        # Track last tick timestamp per symbol so we only broadcast newer data.
        self._last_tick_time: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            logger.warning("StreamManager already running")
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("StreamManager started — background poll loop active")

    async def stop(self) -> None:
        """Cancel the background polling loop."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("StreamManager stopped")

    # ------------------------------------------------------------------
    # Client subscription management (via WebSocket endpoint)
    # ------------------------------------------------------------------

    async def subscribe(self, ws: WebSocket, symbol: str) -> None:
        """Add a WebSocket client to the subscription set for *symbol*."""
        async with self._lock:
            if symbol not in self._subscriptions:
                self._subscriptions[symbol] = set()
            self._subscriptions[symbol].add(ws)
            logger.debug(
                "WS subscribed to %s (total clients for symbol: %d)",
                symbol,
                len(self._subscriptions[symbol]),
            )

    async def unsubscribe(
        self, ws: WebSocket, symbol: str | None = None
    ) -> None:
        """Remove a WebSocket client from one or all subscriptions.

        Args:
            ws: The WebSocket client to remove.
            symbol: If given, remove only from this symbol's set.
                     If None, remove from all symbols.
        """
        async with self._lock:
            if symbol is not None:
                clients = self._subscriptions.get(symbol)
                if clients:
                    clients.discard(ws)
                    if not clients:
                        del self._subscriptions[symbol]
                        self._last_tick_time.pop(symbol, None)
                logger.debug("WS unsubscribed from %s", symbol)
            else:
                for sym, clients in list(self._subscriptions.items()):
                    clients.discard(ws)
                    if not clients:
                        del self._subscriptions[sym]
                        self._last_tick_time.pop(sym, None)
                logger.debug("WS unsubscribed from all symbols")

    # ------------------------------------------------------------------
    # Symbol subscription management (via MCP tools)
    # ------------------------------------------------------------------

    async def subscribe_symbol(self, symbol: str) -> None:
        """Register a symbol for server-side polling via MCP tools.

        Unlike ``subscribe()``, this does not require a WebSocket client.
        The symbol will be polled regardless of whether a client is connected.
        """
        async with self._lock:
            self._symbol_subscriptions.add(symbol)
            logger.debug("Symbol subscription added: %s", symbol)

    async def unsubscribe_symbol(self, symbol: str | None = None) -> None:
        """Remove a symbol from server-side polling.

        Args:
            symbol: If given, remove only this symbol.
                     If None, clear all symbol subscriptions.
        """
        async with self._lock:
            if symbol is not None:
                self._symbol_subscriptions.discard(symbol)
                self._last_tick_time.pop(symbol, None)
                logger.debug("Symbol subscription removed: %s", symbol)
            else:
                self._symbol_subscriptions.clear()
                logger.debug("All symbol subscriptions cleared")

    async def get_subscriptions(self) -> dict[str, Any]:
        """Return a read-only snapshot of current subscriptions."""
        async with self._lock:
            client_subs = {
                symbol: len(clients)
                for symbol, clients in self._subscriptions.items()
            }
            return {
                "by_client": client_subs,
                "by_mcp_tool": sorted(self._symbol_subscriptions),
                "active_polls": sorted(
                    set(self._symbol_subscriptions) | set(self._subscriptions.keys())
                ),
                "client_count": (
                    sum(len(c) for c in self._subscriptions.values())
                    if self._subscriptions
                    else 0
                ),
            }

    # ------------------------------------------------------------------
    # Background poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Continuously poll subscribed symbols and broadcast ticks."""
        while self._running:
            try:
                async with self._lock:
                    # Poll any symbol that has client subscriptions or
                    # server-side subscriptions (via MCP tools).
                    symbols = list(
                        set(self._subscriptions.keys())
                        | self._symbol_subscriptions
                    )

                for symbol in symbols:
                    try:
                        await self._poll_symbol(symbol)
                    except Exception as exc:
                        logger.warning(
                            "StreamManager poll error for %s: %s",
                            symbol,
                            exc,
                        )
                        # Continue with next symbol — one failure shouldn't
                        # break the entire loop.
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "StreamManager poll loop iteration failed: %s", exc
                )

            await asyncio.sleep(1.5)

    async def _poll_symbol(self, symbol: str) -> None:
        """Poll a single symbol for fresh ticks and broadcast."""
        # _tcp_send_and_await is a synchronous function that blocks the
        # calling thread internally via asyncio.run_coroutine_threadsafe.
        # We offload it to a thread executor to avoid blocking the async
        # event loop, which would prevent WebSocket broadcasts.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            _tcp_send_and_await,
            "get_ticks",
            {"symbol": symbol, "count": 1},
            5.0,
        )
        if not result or result.get("status") != "completed":
            return

        payload = result.get("result", {}).get("payload", {})
        if isinstance(payload, str):
            import json

            try:
                data = json.loads(payload)
            except Exception:
                return
        elif isinstance(payload, dict):
            data = payload
        else:
            return

        ticks = data.get("data") or data.get("ticks") or []
        if not ticks:
            return

        tick = ticks[-1] if isinstance(ticks, list) else ticks
        tick_time = tick.get("time") or tick.get("time_msc") or 0

        # Only broadcast if this tick is newer than the last one we sent.
        last_ts = self._last_tick_time.get(symbol, 0.0)
        if tick_time and tick_time <= last_ts:
            return
        if tick_time:
            self._last_tick_time[symbol] = float(tick_time)

        await self._broadcast(symbol, tick)

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def _broadcast(self, symbol: str, tick_data: dict[str, Any]) -> None:
        """Send tick data to all connected clients subscribed to *symbol*.

        Dead clients (send failure) are removed from all subscriptions
        so they don't accumulate.
        """
        async with self._lock:
            clients = self._subscriptions.get(symbol)
            if not clients:
                return
            # Snapshot to iterate safely
            snapshot = clients.copy()

        dead: set[WebSocket] = set()
        message = {
            "type": "tick",
            "symbol": symbol,
            "data": tick_data,
            "timestamp": time.time(),
        }

        for ws in snapshot:
            try:
                await ws.send_json(message)
            except Exception:
                logger.debug(
                    "Removing dead WS client from %s subscriptions", symbol
                )
                dead.add(ws)

        if dead:
            async with self._lock:
                for sym_clients in self._subscriptions.values():
                    sym_clients.difference_update(dead)
                # Clean up empty symbol entries
                for sym in list(self._subscriptions.keys()):
                    if not self._subscriptions[sym]:
                        del self._subscriptions[sym]
                        self._last_tick_time.pop(sym, None)
