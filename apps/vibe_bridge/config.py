"""Configuration for Vibe-Trading integration."""

import os
from pathlib import Path


def get_vibe_trading_dir() -> Path | None:
    """Get path to Vibe-Trading agent directory.

    Returns None if Vibe-Trading directory is not configured or not found.
    """
    env_path = os.getenv("VIBE_TRADING_DIR")
    if env_path:
        return Path(env_path).resolve()
    # Default: sibling directory to MetaTraderBridge
    mtbridge_root = Path(__file__).resolve().parent.parent.parent
    default = (mtbridge_root.parent / "Vibe-Trading" / "agent").resolve()
    if default.exists():
        return default
    return None


def get_vibe_mcp_port() -> int:
    """Get Vibe-Trading MCP SSE port."""
    return int(os.getenv("VIBE_TRADING_MCP_PORT", "8900"))


def get_vibe_env_overrides() -> dict[str, str]:
    """Get environment variable overrides for Vibe-Trading subprocess."""
    env = {}
    if provider := os.getenv("VIBE_TRADING_LLM_PROVIDER"):
        env["LANGCHAIN_PROVIDER"] = provider
    if provider and (base_url := os.getenv("VIBE_TRADING_LLM_BASE_URL")):
        env[f"{provider.upper()}_BASE_URL"] = base_url
    if model := os.getenv("VIBE_TRADING_LLM_MODEL"):
        env["LANGCHAIN_MODEL_NAME"] = model
    if tushare := os.getenv("TUSHARE_TOKEN"):
        env["TUSHARE_TOKEN"] = tushare
    if timeout := os.getenv("VIBE_TRADING_TIMEOUT"):
        env["TIMEOUT_SECONDS"] = timeout
    return env
