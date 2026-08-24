"""Translate Vibe-Trading strategy/research output into MetaTraderBridge-compatible orders.

Vibe-Trading produces research reports, swarm debates, and backtest results.
This module extracts actionable signals and maps them to MetaTraderBridge order schemas.
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


class SignalStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


@dataclass
class TradeSignal:
    """A tradeable signal extracted from Vibe-Trading output."""

    action: SignalAction
    symbol: str  # e.g. "XAUUSD", "EURUSD"
    strength: SignalStrength = SignalStrength.MODERATE
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 0.5  # 0.0 to 1.0
    reasoning: str = ""
    source: str = ""  # e.g. "swarm:investment_committee", "backtest:abc123"
    timeframe: str = "H1"  # MT5 timeframe recommendation
    risk_reward: Optional[float] = None

    def __post_init__(self):
        if self.risk_reward is None and self.entry_price and self.stop_loss and self.take_profit:
            risk = abs(self.entry_price - self.stop_loss)
            reward = abs(self.take_profit - self.entry_price)
            if risk > 0:
                self.risk_reward = round(reward / risk, 2)
        self.symbol = self._map_symbol(self.symbol)

    def to_order_params(self) -> dict[str, str | float]:
        """Convert signal to MetaTraderBridge submit_order parameters."""
        params: dict[str, str | float] = {
            "symbol": self._map_symbol(self.symbol),
            "action": self.action.value,
        }
        if self.entry_price is not None:
            params["price"] = self.entry_price
        if self.stop_loss is not None:
            params["sl"] = self.stop_loss
        if self.take_profit is not None:
            params["tp"] = self.take_profit
        if self.timeframe:
            params["timeframe"] = self.timeframe
        if self.reasoning:
            params["comment"] = f"Vibe: {self.reasoning[:100]}"
        return params

    @staticmethod
    def _map_symbol(symbol: str) -> str:
        """Map Vibe-Trading symbol format to MT5 symbol format."""
        mapping = {
            "XAU/USD": "XAUUSD",
            "XAUUSDM": "XAUUSD",
            "GOLD": "XAUUSD",
            "EUR/USD": "EURUSD",
            "GBP/USD": "GBPUSD",
            "USD/JPY": "USDJPY",
            "BTC-USDT": "BTCUSD",
            "ETH-USDT": "ETHUSD",
        }
        return mapping.get(symbol.upper(), symbol.upper().replace("/", "").replace("-", ""))


def extract_signal_from_swarm_report(report: str, preset: str = "") -> Optional[TradeSignal]:
    """Extract trade signal from a swarm team final report.

    Parses the report for actionable trade recommendations with
    entry/exit levels.
    """
    action_match = re.search(r"\b(BUY|SELL|CLOSE)\b\s+([A-Z]{3,8}[/\-]?[A-Z]{3})", report, re.I)
    if not action_match:
        return None

    action = SignalAction(action_match.group(1).upper())
    raw_symbol = action_match.group(2)

    entry = _extract_price(report, r"(?:entry|enter|open)\s*(?:at|price)?:?\s*\$?([\d,]+\.?\d*)")
    sl = _extract_price(report, r"(?:stop[\s-]?loss|SL|stop)\s*(?:at|:)?\s*\$?([\d,]+\.?\d*)")
    tp = _extract_price(report, r"(?:take[\s-]?profit|TP|target)\s*(?:at|:)?\s*\$?([\d,]+\.?\d*)")

    confidence = _extract_confidence(report)
    reasoning = _extract_reasoning(report)

    risk_reward = None
    if entry and sl and tp and entry != sl:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk > 0:
            risk_reward = round(reward / risk, 2)

    strength = (
        SignalStrength.STRONG
        if confidence >= 0.75
        else (SignalStrength.MODERATE if confidence >= 0.5 else SignalStrength.WEAK)
    )

    return TradeSignal(
        action=action,
        symbol=raw_symbol,
        strength=strength,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        confidence=confidence,
        reasoning=reasoning,
        source=f"swarm:{preset}" if preset else "swarm",
        risk_reward=risk_reward,
    )


def extract_signal_from_backtest(result: str, symbol: str = "") -> Optional[TradeSignal]:
    """Extract trade viability from backtest results.

    If backtest shows positive Sharpe + acceptable drawdown,
    generate a HOLD/BUY signal to proceed with live execution.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None

    sharpe = _safe_float(data.get("sharpe_ratio") or data.get("sharpe"))
    max_dd = _safe_float(data.get("max_drawdown") or data.get("max_dd"))
    win_rate = _safe_float(data.get("win_rate") or data.get("win_rate_pct"))
    total_return = _safe_float(data.get("total_return") or data.get("return_pct"))

    if sharpe is None and total_return is None:
        return None

    if (sharpe is not None and sharpe > 1.0) or (total_return is not None and total_return > 0):
        confidence = min(0.9, max(0.3, (sharpe or 0) / 3.0 + 0.3))
        action = SignalAction.BUY if (total_return or 0) > 0 else SignalAction.SELL
        reasoning = f"Backtest: Sharpe={sharpe}, MaxDD={max_dd}%, WR={win_rate}%, Return={total_return}%"
        return TradeSignal(
            action=action,
            symbol=symbol or "UNKNOWN",
            strength=SignalStrength.MODERATE if sharpe and sharpe > 1.0 else SignalStrength.WEAK,
            confidence=confidence,
            reasoning=reasoning,
            source="backtest",
        )

    return TradeSignal(
        action=SignalAction.HOLD,
        symbol=symbol or "UNKNOWN",
        confidence=0.2,
        reasoning=f"Backtest not compelling: Sharpe={sharpe}, MaxDD={max_dd}%",
        source="backtest",
    )


def _extract_price(text: str, pattern: str) -> Optional[float]:
    """Extract a price value from text using regex pattern."""
    match = re.search(pattern, text, re.I)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except (ValueError, IndexError):
            return None
    return None


def _extract_confidence(text: str) -> float:
    """Extract confidence level from text."""
    match = re.search(r"confidence[:\s]+(\d+)%", text, re.I)
    if match:
        return int(match.group(1)) / 100.0
    match = re.search(r"confidence[:\s]+(0\.\d+)", text, re.I)
    if match:
        return float(match.group(1))
    return 0.5


def _extract_reasoning(text: str) -> str:
    """Extract first 200 chars of reasoning from text."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:200]


def _safe_float(value) -> Optional[float]:
    """Safely convert value to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
