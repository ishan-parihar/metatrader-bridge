from __future__ import annotations

import asyncio
import os
import struct
import time
import uuid
from asyncio import StreamReader, StreamWriter
from typing import Any

import httpx

from mt5_mcp.observability.logging import setup_logging, logger

from .protocol import FrameParser, encode_frame

setup_logging()

# Max payload: 16 MB
_MAX_PAYLOAD = 16 * 1024 * 1024
_HEADER_FORMAT = "!I"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)

# Wine socket bug: Wine may inject HTTP data into raw TCP socket buffers.
# Detect common HTTP method prefixes to discard garbage before frame parsing.
_HTTP_METHODS = (
    b"GET ",
    b"POST",
    b"PUT ",
    b"HEAD",
    b"DEL",
    b"OPTI",
    b"PATC",
    b"CONN",
    b"TRAC",
)


def _discard_http_garbage(buffer: bytearray) -> bool:
    if len(buffer) < 4:
        return False
    prefix = bytes(buffer[:4])
    if prefix.startswith(_HTTP_METHODS):
        try:
            body_start = buffer.index(b"\r\n\r\n")
            del buffer[: body_start + 4]
        except ValueError:
            buffer.clear()
        return True
    return False


class PendingCommand:
    __slots__ = ("request_id", "type", "payload", "future", "enqueued_at")

    def __init__(self, request_id: str, type: str, payload: dict[str, Any]):
        self.request_id = request_id
        self.type = type
        self.payload = payload
        self.future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.enqueued_at = time.monotonic()


class TCPBridgeServer:
    """Asyncio TCP bridge server for low-latency EA communication.

    Architecture:
    - Port 8025 (EA port): Single persistent connection from the MQL5 EA.
      EA sends heartbeats + results here. Server pushes commands here.

    Commands from in-process callers (MCP tools, via enqueue_and_await)
    are pushed instantly to EA (no polling).
    Results from EA are pushed instantly to callers (no polling).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        ea_port: int = 8025,
        gateway_url: str = "http://127.0.0.1:8010",
    ):
        self._host = host
        self._ea_port = ea_port
        self._gateway_url = gateway_url
        self._ea_server: asyncio.Server | None = None
        self._ea_writer: StreamWriter | None = None
        self._ea_parser = FrameParser()
        self._pending: dict[str, PendingCommand] = {}
        self._ea_connected = False
        self._ea_address: str = ""

        self._http_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._ea_server = await asyncio.start_server(
            self._handle_ea_connection, self._host, self._ea_port
        )
        addr = self._ea_server.sockets[0].getsockname()
        logger.info(f"EA listener on {addr}")

        async with self._ea_server:
            await self._ea_server.serve_forever()

    async def stop(self) -> None:
        """Gracefully shutdown the TCP bridge server."""
        logger.info("Shutting down TCP Bridge Server...")

        # Close EA connection
        if self._ea_writer and not self._ea_writer.is_closing():
            self._ea_writer.close()
            try:
                await self._ea_writer.wait_closed()
            except Exception:
                pass

        # Close HTTP client
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        # Fail any pending commands
        self._fail_pending_on_disconnect()

        # Servers are closed via async context manager in start()
        logger.info("TCP Bridge Server stopped")

    async def _handle_ea_connection(
        self, reader: StreamReader, writer: StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        self._ea_address = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        self._ea_writer = writer
        self._ea_connected = True
        self._ea_parser.reset()
        logger.info(f"EA connected from {self._ea_address}")
        self._drain_command_queue()

        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    logger.info(f"EA closed connection")
                    break
                self._ea_parser.feed(data)
                while True:
                    header = self._ea_parser._peek_header()
                    if header and _discard_http_garbage(self._ea_parser._buffer):
                        logger.warning(
                            f"Discarded Wine HTTP garbage from EA connection: "
                            f"{header!r}"
                        )
                        self._ea_parser.clear()
                        continue
                    if not self._ea_parser.has_frame():
                        break
                    frame = self._ea_parser.pop_frame()
                    await self._handle_frame(frame)
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"EA connection error: {e}")
        finally:
            self._ea_connected = False
            self._ea_writer = None
            self._fail_pending_on_disconnect()
            writer.close()
            await writer.wait_closed()
            logger.info(f"EA disconnected: {self._ea_address}")

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Process a frame received from the EA (results or heartbeats)."""
        request_id = frame.get("request_id", "")

        if request_id and request_id in self._pending:
            cmd = self._pending.pop(request_id)
            status = frame.get("status", "ok")
            if status == "ok":
                cmd.future.set_result(frame)
            else:
                cmd.future.set_exception(
                    RuntimeError(frame.get("error", "unknown error"))
                )
            latency_ms = (time.monotonic() - cmd.enqueued_at) * 1000
            logger.info(f"Command {cmd.type} completed in {latency_ms:.1f}ms")

        elif frame.get("type") == "heartbeat":
            logger.debug(f"EA heartbeat via TCP")
            # Forward heartbeat to HTTP gateway so EABridgeAdapter sees it
            await self._forward_heartbeat_to_gateway(frame)
        else:
            logger.warning(f"Unsolicited frame from EA: {frame}")
            if request_id and self._ea_writer and not self._ea_writer.is_closing():
                try:
                    ack_frame = {
                        "request_id": request_id,
                        "status": "ok",
                        "type": "ack",
                        "message": "result received but request already timed out",
                    }
                    self._ea_writer.write(encode_frame(ack_frame))
                    await self._ea_writer.drain()
                except Exception as e:
                    logger.warning(f"Failed to send ACK for orphaned frame: {e}")

    async def _forward_heartbeat_to_gateway(self, frame: dict[str, Any]) -> None:
        """Relay TCP heartbeat to HTTP gateway so EABridgeAdapter sees EA as connected."""
        try:
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(timeout=5.0)
            payload = {k: v for k, v in frame.items() if k != "type"}
            await self._http_client.post(
                f"{self._gateway_url}/bridge/terminal/heartbeat",
                json=payload,
            )
        except Exception as e:
            logger.warning(f"Failed to forward heartbeat to gateway: {e}")

    def _drain_command_queue(self) -> None:
        """Flush queued commands when EA reconnects."""
        pass  # Queue drain removed — commands fail fast with ConnectionError when EA is down

    def _fail_pending_on_disconnect(self) -> None:
        for cmd in list(self._pending.values()):
            if not cmd.future.done():
                cmd.future.set_exception(ConnectionError("EA disconnected"))
        self._pending.clear()

    async def enqueue_and_await(
        self, type: str, payload: dict[str, Any], timeout: float = 20.0
    ) -> dict[str, Any]:
        """Enqueue a command and await the EA's response. Used by in-process callers."""
        if not self._ea_connected:
            raise ConnectionError("EA not connected to TCP bridge")

        request_id = str(uuid.uuid4())
        frame = {"type": type, "request_id": request_id, **payload}

        cmd = PendingCommand(request_id, type, payload)
        self._pending[request_id] = cmd

        try:
            data = encode_frame(frame)
            self._ea_writer.write(data)
            await self._ea_writer.drain()
        except Exception as e:
            self._pending.pop(request_id, None)
            raise ConnectionError(f"Failed to send command: {e}")

        try:
            return await asyncio.wait_for(cmd.future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise

    @property
    def ea_connected(self) -> bool:
        return self._ea_connected

    @property
    def pending_count(self) -> int:
        return len(self._pending)


_bridge_server: TCPBridgeServer | None = None


def get_bridge_server() -> TCPBridgeServer:
    global _bridge_server
    if _bridge_server is None:
        ea_port = int(os.getenv("MT5_TCP_BRIDGE_PORT", "8025"))
        gateway_url = os.getenv("MT5_GATEWAY_URL", "http://127.0.0.1:8010")
        _bridge_server = TCPBridgeServer(
            ea_port=ea_port, gateway_url=gateway_url
        )
    return _bridge_server
