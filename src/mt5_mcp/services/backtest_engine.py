"""Native backtesting engine for strategy validation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def compute_sma(prices: list[float], period: int) -> list[float | None]:
    if len(prices) < period:
        return [None] * len(prices)
    result: list[float | None] = [None] * (period - 1)
    for i in range(period - 1, len(prices)):
        result.append(sum(prices[i - period + 1 : i + 1]) / period)
    return result


def compute_ema(prices: list[float], period: int) -> list[float | None]:
    if len(prices) < period:
        return [None] * len(prices)
    multiplier = 2.0 / (period + 1)
    result: list[float | None] = [None] * (period - 1)
    ema = sum(prices[:period]) / period
    result.append(ema)
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
        result.append(ema)
    return result


def run_backtest(
    symbol: str,
    timeframe: str,
    strategy_name: str,
    bars_data: list[dict],
    params: dict[str, Any] | None = None,
    initial_balance: float = 10000.0,
    volume_lots: float = 0.1,
) -> dict[str, Any]:
    params = params or {}
    backtest_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    closes = [float(b["close"]) for b in bars_data]
    highs = [float(b["high"]) for b in bars_data]
    lows = [float(b["low"]) for b in bars_data]
    times = [b.get("time") for b in bars_data]

    trades: list[dict] = []
    equity_curve: list[dict] = []
    balance = initial_balance
    position: dict | None = None

    pip_value = volume_lots * 1.0

    if strategy_name in ("sma_cross", "ma_cross"):
        fast_period = int(params.get("fast_period", 10))
        slow_period = int(params.get("slow_period", 30))
        fast_ma = compute_sma(closes, fast_period)
        slow_ma = compute_sma(closes, slow_period)

        for i in range(1, len(bars_data)):
            f_prev = fast_ma[i - 1]
            s_prev = slow_ma[i - 1]
            f_cur = fast_ma[i]
            s_cur = slow_ma[i]
            if f_prev is None or s_prev is None or f_cur is None or s_cur is None:
                equity_curve.append({"time": str(times[i] or ""), "balance": round(balance, 2), "equity": round(balance, 2)})
                continue

            if f_prev <= s_prev and f_cur > s_cur:
                if position is None:
                    position = {"side": "buy", "entry_price": closes[i], "entry_time": str(times[i]), "volume": volume_lots}
            elif f_prev >= s_prev and f_cur < s_cur:
                if position is not None:
                    pips = (closes[i] - position["entry_price"]) / 0.0001
                    pnl = pips * pip_value
                    balance += pnl
                    trades.append({
                        "side": "buy", "entry_price": position["entry_price"], "exit_price": closes[i],
                        "entry_time": position["entry_time"], "exit_time": str(times[i] or ""),
                        "pnl": round(pnl, 2), "pips": round(pips, 1),
                    })
                    position = None

            equity_curve.append({"time": str(times[i] or ""), "balance": round(balance, 2), "equity": round(balance, 2)})

    elif strategy_name == "ema_cross":
        fast_period = int(params.get("fast_period", 12))
        slow_period = int(params.get("slow_period", 26))
        fast_ma = compute_ema(closes, fast_period)
        slow_ma = compute_ema(closes, slow_period)

        for i in range(1, len(bars_data)):
            f_prev = fast_ma[i - 1]
            s_prev = slow_ma[i - 1]
            f_cur = fast_ma[i]
            s_cur = slow_ma[i]
            if f_prev is None or s_prev is None or f_cur is None or s_cur is None:
                equity_curve.append({"time": str(times[i] or ""), "balance": round(balance, 2), "equity": round(balance, 2)})
                continue

            if f_prev <= s_prev and f_cur > s_cur:
                if position is None:
                    position = {"side": "buy", "entry_price": closes[i], "entry_time": str(times[i]), "volume": volume_lots}
            elif f_prev >= s_prev and f_cur < s_cur:
                if position is not None:
                    pips = (closes[i] - position["entry_price"]) / 0.0001
                    pnl = pips * pip_value
                    balance += pnl
                    trades.append({
                        "side": "buy", "entry_price": position["entry_price"], "exit_price": closes[i],
                        "entry_time": position["entry_time"], "exit_time": str(times[i] or ""),
                        "pnl": round(pnl, 2), "pips": round(pips, 1),
                    })
                    position = None

            equity_curve.append({"time": str(times[i] or ""), "balance": round(balance, 2), "equity": round(balance, 2)})

    elif strategy_name == "breakout":
        lookback = int(params.get("lookback", 20))
        for i in range(lookback, len(bars_data)):
            recent_high = max(highs[i - lookback : i])
            recent_low = min(lows[i - lookback : i])

            if closes[i] > recent_high and position is None:
                position = {"side": "buy", "entry_price": closes[i], "entry_time": str(times[i]), "volume": volume_lots}
            elif closes[i] < recent_low and position is not None:
                pips = (closes[i] - position["entry_price"]) / 0.0001
                pnl = pips * pip_value
                balance += pnl
                trades.append({
                    "side": "buy", "entry_price": position["entry_price"], "exit_price": closes[i],
                    "entry_time": position["entry_time"], "exit_time": str(times[i] or ""),
                    "pnl": round(pnl, 2), "pips": round(pips, 1),
                })
                position = None

            equity_curve.append({"time": str(times[i] or ""), "balance": round(balance, 2), "equity": round(balance, 2)})

    if position is not None and bars_data:
        exit_price = closes[-1]
        pips = (exit_price - position["entry_price"]) / 0.0001
        pnl = pips * pip_value
        balance += pnl
        trades.append({
            "side": "buy", "entry_price": position["entry_price"], "exit_price": exit_price,
            "entry_time": position["entry_time"], "exit_time": str(times[-1] or ""),
            "pnl": round(pnl, 2), "pips": round(pips, 1),
        })

    total_trades = len(trades)
    winning = [t for t in trades if t["pnl"] > 0]
    losing = [t for t in trades if t["pnl"] <= 0]
    win_count = len(winning)
    lose_count = len(losing)
    total_pnl = balance - initial_balance
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0
    gross_profit = sum(t["pnl"] for t in winning)
    gross_loss = abs(sum(t["pnl"] for t in losing))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    peak = initial_balance
    max_dd = 0.0
    for eq in equity_curve:
        eq_val = eq["equity"]
        if eq_val > peak:
            peak = eq_val
        dd = (peak - eq_val) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    returns = [t["pnl"] / initial_balance for t in trades] if initial_balance > 0 else []
    avg_return = sum(returns) / len(returns) if returns else 0.0
    std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5 if len(returns) > 1 else 0.0
    sharpe = (avg_return / std_return * (252 ** 0.5)) if std_return > 0 else 0.0

    completed_at = datetime.now(timezone.utc).isoformat()

    return {
        "backtest_id": backtest_id,
        "status": "completed",
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_name": strategy_name,
        "total_trades": total_trades,
        "winning_trades": win_count,
        "losing_trades": lose_count,
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 4),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "trades": trades[-100:],
        "equity_curve": equity_curve[::max(1, len(equity_curve) // 500)],
        "started_at": started_at,
        "completed_at": completed_at,
    }
