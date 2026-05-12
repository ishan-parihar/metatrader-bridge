"""
Reusable bridge protocol for MCP servers that need to communicate
with the TCP bridge. Supports direct TCP connection (standalone mode)
or in-process singleton (combined mode).

Protocol:
  Request:  {"id": "<req_id>", "command": "<command>", "payload": {...}}\\n
  Response: {"request_id": "<req_id>", "result": {...}}\\n

Port 8026 is used for remote TCP client connections (standalone MCP servers).
Port 8025 is the in-process bridge used by the combined server.

Usage:
    protocol = BridgeProtocol(host="127.0.0.1", port=8026)
    await protocol.connect()
    result = await protocol.send_and_await("get_bars", {"symbol": "EURUSD", ...})
    await protocol.disconnect()

Or as context manager:
    async with BridgeProtocol() as protocol:
        result = await protocol.send_and_await("get_bars", {...})
"""

import asyncio
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class BridgeProtocol:
    """
    Reusable bridge protocol for MCP servers that need to communicate
    with the TCP bridge. Supports direct TCP connection (standalone mode)
    or in-process singleton (combined mode).

    Uses newline-delimited JSON for framing (one JSON object per line),
    making it easy to debug with tools like ``nc`` or ``telnet``.

    Handles:
    - Automatic reconnection on disconnect
    - Request/response matching via unique request IDs
    - Concurrent pending requests with per-request timeouts
    - Graceful shutdown (cancels pending futures on disconnect)
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8026, timeout: float = 5.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._connected = False
        self._request_id = 0
        self._listen_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        """Connect to the TCP bridge.

        Returns True if connection succeeded, False otherwise.

        Starts a background listen loop that reads newline-delimited JSON
        responses and dispatches them to the matching pending future.
        """
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
            self._connected = True
            self._listen_task = asyncio.create_task(self._listen_loop())
            logger.info(f"BridgeProtocol connected to {self._host}:{self._port}")
            return True
        except Exception as e:
            logger.error(f"BridgeProtocol connect failed: {e}")
            self._connected = False
            return False

    async def disconnect(self):
        """Disconnect from the TCP bridge.

        Cancels the background listen loop, closes the TCP connection,
        and fails all pending futures with ConnectionError.
        """
        self._connected = False
        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        # Fail all pending futures so no caller hangs indefinitely
        for req_id, future in self._pending.items():
            if not future.done():
                future.set_exception(ConnectionError("Bridge disconnected"))
        self._pending.clear()

    async def send_and_await(
        self, command: str, payload: Optional[dict] = None
    ) -> Any:
        """Send a command and await the response.

        Automatically connects if not already connected.
        Returns the response payload, or a dict with an ``error`` key on failure.

        Args:
            command: The bridge command name (e.g. ``"get_bars"``, ``"get_ticks"``).
            payload: Optional dict of command parameters.

        Returns:
            The response result, or ``{"error": "..."}`` on failure.
        """
        if not self._connected:
            ok = await self.connect()
            if not ok:
                return {"error": "Bridge not connected"}

        async with self._lock:
            self._request_id += 1
            req_id = str(self._request_id)
            request = {
                "id": req_id,
                "command": command,
                "payload": payload or {},
            }
            future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future

        try:
            writer = self._writer
            if writer is None:
                self._pending.pop(req_id, None)
                return {"error": "Bridge not connected (writer is None)"}
            data = (json.dumps(request) + "\n").encode("utf-8")
            writer.write(data)
            await writer.drain()

            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": f"Timeout waiting for response to {command}"}
        except ConnectionError as e:
            self._pending.pop(req_id, None)
            return {"error": f"Connection lost: {e}"}
        except Exception as e:
            self._pending.pop(req_id, None)
            return {"error": str(e)}

    async def _listen_loop(self):
        """Background listener for bridge responses.

        Reads newline-delimited JSON from the TCP connection and dispatches
        each response to the corresponding pending future by matching the
        ``request_id`` (or ``id``) field.

        Runs until the connection is closed or cancelled.
        """
        buffer = ""
        while self._connected and self._reader:
            try:
                line = await asyncio.wait_for(
                    self._reader.readline(),
                    timeout=60,  # Long timeout — just a heartbeat guard
                )
                if not line:
                    logger.warning("Bridge connection closed by server")
                    await self.disconnect()
                    break

                raw_line = line.decode("utf-8").strip()
                if not raw_line:
                    continue

                response = json.loads(raw_line)
                # Support both 'request_id' and 'id' as the correlation field
                req_id = response.get("request_id") or response.get("id")
                if req_id and req_id in self._pending:
                    future = self._pending.pop(req_id)
                    if not future.done():
                        future.set_result(response.get("result", response))

            except asyncio.TimeoutError:
                continue  # Normal timeout on readline — keep listening
            except json.JSONDecodeError as e:
                logger.warning(
                    f"BridgeProtocol JSON parse error: {e}, data={buffer[:200]}"
                )
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"BridgeProtocol listen error: {e}")
                await self.disconnect()
                break

    @property
    def is_connected(self) -> bool:
        """Check if the protocol has an active TCP connection."""
        return self._connected

    # Context manager support for clean resource management:
    #
    #     async with BridgeProtocol() as bp:
    #         result = await bp.send_and_await("get_bars", {...})
    #
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()
