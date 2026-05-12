from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from mt5_mcp.observability.logging import setup_logging, logger

setup_logging()

# Vibe-Trading lifecycle
from apps.vibe_bridge.lifecycle import VibeBridgeLifecycle

_vibe_lifecycle: Optional[VibeBridgeLifecycle] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage Vibe-Trading subprocess lifecycle on startup/shutdown."""
    global _vibe_lifecycle
    _vibe_lifecycle = VibeBridgeLifecycle()

    if os.getenv("VIBE_TRADING_AUTO_START", "false").lower() == "true":
        try:
            started = await _vibe_lifecycle.start()
            if started:
                logger.info("Vibe-Trading auto-started on gateway startup")
            else:
                logger.warning("Vibe-Trading auto-start failed")
        except Exception as e:
            logger.warning("Failed to auto-start Vibe-Trading", error=str(e))
    else:
        logger.info(
            "Vibe-Trading auto-start disabled (set VIBE_TRADING_AUTO_START=true to enable)"
        )

    yield

    if _vibe_lifecycle:
        await _vibe_lifecycle.stop()
        logger.info("Vibe-Trading lifecycle stopped")


app = FastAPI(title="MT5 Bridge Gateway", version="0.1.0", lifespan=lifespan)

from apps.bridge_gateway.router import router

app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8020)
