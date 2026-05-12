from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field


def _parse_strategy_magic_numbers() -> dict[str, int]:
    try:
        return json.loads(os.environ.get("MT5_STRATEGY_MAGIC_NUMBERS", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_idempotency_ttl() -> int:
    try:
        return int(os.environ.get("MT5_IDEMPOTENCY_TTL_SECONDS", "86400"))
    except ValueError:
        return 86400


@dataclass(frozen=True)
class Settings:
    mt5_terminal_path: str = os.getenv(
        "MT5_TERMINAL_PATH",
        "/home/ishanp/.local/share/bottles/bottles/Apps/drive_c/Program Files/MetaTrader 5",
    )
    environment: str = os.getenv("MT5_ENV", "demo")
    execution_mode: str = os.getenv("MT5_EXECUTION_MODE", "human_approval_required")
    adapter: str = os.getenv("MT5_ADAPTER", "pymt5")
    gateway_url: str = os.getenv("MT5_GATEWAY_URL", "http://127.0.0.1:8010")
    redis_url: str = os.getenv("MT5_REDIS_URL", "redis://localhost:6379/0")
    symbol_suffix: str = os.getenv("MT5_SYMBOL_SUFFIX", "m")
    strategy_magic_numbers: dict[str, int] = field(
        default_factory=_parse_strategy_magic_numbers
    )
    idempotency_ttl_seconds: int = field(default_factory=_parse_idempotency_ttl)


def derive_magic_number(strategy_id: str) -> int:
    if not strategy_id or not strategy_id.strip():
        raise ValueError("strategy_id cannot be empty or whitespace")
    raw = int(hashlib.md5(strategy_id.encode()).hexdigest()[:8], 16)
    return max(1, raw % 4294967296)


def compose_comment(strategy_id: str, intent_id: str, session_id: str) -> str:
    """Compose a deterministic MT5 order comment, truncated to 31 characters."""
    comment = f"{strategy_id}:{intent_id}:{session_id}"
    return comment[:31]


def get_settings() -> Settings:
    return Settings()
