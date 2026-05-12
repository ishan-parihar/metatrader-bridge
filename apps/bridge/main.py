"""
MT5 Bridge Service — Unified entry point for all MT5 bridge components.

Combines:
1. TCP Bridge Server (port 8025) — Low-latency TCP push to MT5 EA
2. HTTP Gateway (port 8020) — HTTP API + command queue
3. MCP Server (port 8010) — Model Context Protocol API

Single service, single process, 3 ports.
"""

from __future__ import annotations

import asyncio
import os
import signal

from mt5_mcp.observability.logging import setup_logging, logger

setup_logging()


async def run_tcp_bridge(stop_event: asyncio.Event):
    """Start TCP Bridge server on port 8025 (EA connection only)."""
    from apps.tcp_bridge.server import get_bridge_server

    ea_port = int(os.getenv("MT5_TCP_BRIDGE_PORT", "8025"))
    server = get_bridge_server()

    await server.start()
    logger.info(f"TCP Bridge running — EA:{ea_port}")
    await stop_event.wait()
    await server.stop()
    logger.info("TCP Bridge stopped")


async def run_http_gateway(stop_event: asyncio.Event):
    """Start HTTP Gateway on port 8020."""
    import uvicorn
    from apps.bridge_gateway.main import app

    bind_host = os.getenv("MT5_GATEWAY_HOST", "127.0.0.1")
    bind_port = int(os.getenv("MT5_GATEWAY_PORT", "8020"))

    config = uvicorn.Config(
        app,
        host=bind_host,
        port=bind_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # Override shutdown trigger
    server.install_signal_handlers = lambda: None
    logger.info(f"HTTP Gateway starting on {bind_host}:{bind_port}")
    asyncio.create_task(server.serve())
    await stop_event.wait()
    await server.shutdown()
    logger.info("HTTP Gateway stopped")


async def run_mcp_server(stop_event: asyncio.Event):
    """Start MCP Server on port 8010."""
    import uvicorn

    # Wire TCP bridge server for in-process MCP tool calls
    from apps.mcp_server.shared import set_bridge_server
    from apps.tcp_bridge.server import get_bridge_server as get_tcp_bridge

    set_bridge_server(get_tcp_bridge(), asyncio.get_running_loop())

    from apps.mcp_server.main import app

    bind_host = os.getenv("MT5_MCP_HOST", "127.0.0.1")
    bind_port = int(os.getenv("MT5_MCP_PORT", "8010"))

    config = uvicorn.Config(
        app,
        host=bind_host,
        port=bind_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    logger.info(f"MCP Server starting on {bind_host}:{bind_port}")
    asyncio.create_task(server.serve())
    await stop_event.wait()
    await server.shutdown()
    logger.info("MCP Server stopped")


def main():
    logger.info("=" * 60)
    logger.info("MT5 Bridge Service — TCP + HTTP + MCP")
    logger.info("=" * 60)

    stop_event = asyncio.Event()

    def handle_signal(sig, frame):
        logger.info(f"Signal {sig} received, shutting down bridge...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def run_all():
        await asyncio.gather(
            run_tcp_bridge(stop_event),
            run_mcp_server(stop_event),
        )

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        logger.info("MT5 Bridge Service stopped")


if __name__ == "__main__":
    main()
