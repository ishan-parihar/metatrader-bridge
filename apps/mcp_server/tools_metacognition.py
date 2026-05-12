

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations

from . import mcp

_META_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


@mcp.tool(name="mt5_log_trade_decision", annotations=_META_ANNOTATIONS)
def mt5_log_trade_decision(
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    action: Optional[str] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    volume_lots: Optional[float] = None,
    pnl: Optional[float] = None,
    session_id: Optional[str] = None,
    regime: Optional[str] = None,
    atr_value: Optional[float] = None,
    atr_percent_of_price: Optional[float] = None,
    rsi_value: Optional[float] = None,
    indicator_snapshot: Optional[dict] = None,
    model_justification: Optional[str] = None,
    indicators_considered: Optional[list] = None,
    confidence_level: Optional[float] = None,
    risk_assessment: Optional[str] = None,
    emotional_self_report: Optional[str] = None,
    alternatives_considered: Optional[str] = None,
    expected_duration: Optional[str] = None,
    expected_move_points: Optional[float] = None,
    outcome: Optional[str] = None,
    lesson_learned: Optional[str] = None,
    would_do_differently: Optional[str] = None,
    mistake_category: Optional[str] = None,
    quality_rating: Optional[int] = None,
    decision_id: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Log a new trade decision or update an existing one in the trading journal.

    Use this for AI agent self-reflection — record the reasoning behind every
    trade including market regime, indicators considered, confidence level,
    and emotional state. Pass a decision_id to update a previous decision
    with its outcome, P&L, and lessons learned. This creates an auditable
    decision trail for post-session review via mt5_reflect_on_trades.

    Args:
        symbol (Optional[str]): Trading symbol (e.g. "EURUSD"). Required for
            new entries. (default: None → "UNKNOWN")
        side (Optional[str]): Trade direction — "buy" or "sell". Required for
            new entries. (default: None → "UNKNOWN")
        action (Optional[str]): Action taken — e.g. "entry", "exit",
            "modify_sl", "modify_tp", "cancel". (default: None → "UNKNOWN")
        entry_price (Optional[float]): Entry price of the trade. (default: None)
        exit_price (Optional[float]): Exit/closing price. Used with decision_id
            for outcome updates. (default: None)
        sl (Optional[float]): Stop loss price placed. (default: None)
        tp (Optional[float]): Take profit price placed. (default: None)
        volume_lots (Optional[float]): Trade volume in lots. (default: None)
        pnl (Optional[float]): Profit/loss in account currency. Used with
            decision_id for outcome updates. (default: None)
        session_id (Optional[str]): AI agent session identifier for grouping
            related decisions. (default: None)
        regime (Optional[str]): Market regime at entry — e.g. "trending_up",
            "ranging", "volatile", "low_liquidity". (default: None)
        atr_value (Optional[float]): ATR (Average True Range) value at time of
            decision. (default: None)
        atr_percent_of_price (Optional[float]): ATR as percentage of current
            price. (default: None)
        rsi_value (Optional[float]): RSI indicator value at decision time.
            (default: None)
        indicator_snapshot (Optional[dict]): Snapshot of all technical
            indicators considered. (default: None)
        model_justification (Optional[str]): Natural language reasoning for the
            decision. Critical for post-trade review. (default: None)
        indicators_considered (Optional[list]): List of indicator names or
            values that influenced the decision. (default: None)
        confidence_level (Optional[float]): Confidence score 0.0-1.0.
            (default: None)
        risk_assessment (Optional[str]): Qualitative risk description — e.g.
            "low", "medium", "high". (default: None)
        emotional_self_report (Optional[str]): Agent's emotional state — e.g.
            "confident", "hesitant", "uncertain". Useful for detecting bias
            patterns. (default: None)
        alternatives_considered (Optional[str]): Description of other actions
            considered before deciding. (default: None)
        expected_duration (Optional[str]): Expected trade duration — e.g.
            "1-2 hours", "intraday", "swing". (default: None)
        expected_move_points (Optional[float]): Expected price move in points.
            (default: None)
        outcome (Optional[str]): Actual outcome — "win", "loss", "breakeven",
            "open". Used with decision_id for updates. (default: None)
        lesson_learned (Optional[str]): What the agent learned from this trade.
            Used with decision_id for updates. (default: None)
        would_do_differently (Optional[str]): How the agent would change the
            approach retrospectively. Used with decision_id. (default: None)
        mistake_category (Optional[str]): Category of mistake if applicable —
            e.g. "fomo", "revenge_trading", "ignored_sl", "bad_rr",
            "overtrading". Used with decision_id. (default: None)
        quality_rating (Optional[int]): Decision quality rating 1-10.
            Used with decision_id for post-hoc evaluation. (default: None)
        decision_id (Optional[str]): If provided, updates the existing decision
            with that ID instead of creating a new one. Use the ID returned
            from the initial log call. (default: None)
        note (Optional[dict]): Additional free-form notes. (default: None)

    Returns:
        dict: Log result with keys:
            - status (str): "logged" for new entries, "updated" for updates,
              "not_found" if decision_id does not exist
            - decision_id (str): Unique identifier for the decision record.
              Save this to update with outcome later.
            - message (str, on new entries): Confirmation message including
              instruction to use decision_id for outcome updates
            - error (str, present on failure): Error description

    Example: mt5_log_trade_decision(symbol="EURUSD", side="buy",
        action="entry", entry_price=1.1050, sl=1.1000, tp=1.1150,
        model_justification="Bullish breakout above resistance",
        confidence_level=0.75)

    Note: This tool does NOT execute trades — it is a journaling tool for
    recording reasoning AFTER or BEFORE trade execution. Use trading tools
    to place orders. Journal entries persist across sessions in the trade
    journal database.
    """
    try:
        from mt5_mcp.services.trade_journal_db import get_journal_db

        journal = get_journal_db()
    except Exception as e:
        return {"status": "error", "message": f"Journal unavailable: {e}"}

    try:
        if decision_id:
            updated = journal.update_decision(
                decision_id,
                exit_price=exit_price,
                pnl=pnl,
                outcome=outcome,
                lesson_learned=lesson_learned,
                would_do_differently=would_do_differently,
                mistake_category=mistake_category,
                quality_rating=quality_rating,
                note=note,
            )
            return {
                "status": "updated" if updated else "not_found",
                "decision_id": decision_id,
            }

        result_id = journal.log_decision(
            symbol=symbol or "UNKNOWN",
            side=side or "UNKNOWN",
            action=action or "UNKNOWN",
            entry_price=entry_price,
            exit_price=exit_price,
            sl=sl,
            tp=tp,
            volume_lots=volume_lots,
            pnl=pnl,
            session_id=session_id,
            regime=regime,
            atr_value=atr_value,
            atr_percent_of_price=atr_percent_of_price,
            rsi_value=rsi_value,
            indicator_snapshot=indicator_snapshot,
            model_justification=model_justification,
            indicators_considered=indicators_considered,
            confidence_level=confidence_level,
            risk_assessment=risk_assessment,
            emotional_self_report=emotional_self_report,
            alternatives_considered=alternatives_considered,
            expected_duration=expected_duration,
            expected_move_points=expected_move_points,
            outcome=outcome,
            lesson_learned=lesson_learned,
            would_do_differently=would_do_differently,
            mistake_category=mistake_category,
            quality_rating=quality_rating,
            note=note,
        )
        return {
            "status": "logged",
            "decision_id": result_id,
            "message": "Decision logged. Use this ID to update with outcome later.",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_reflect_on_trades", annotations=_META_ANNOTATIONS)
def mt5_reflect_on_trades(
    symbol: Optional[str] = None,
    outcome: Optional[str] = None,
    regime: Optional[str] = None,
    emotional_self_report: Optional[str] = None,
    mistake_category: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Query the trading journal to retrieve past trade decisions for review.

    Use this for AI agent post-session reflection — search past decisions by
    symbol, outcome, market regime, emotional state, or mistake category to
    identify patterns and improve future performance. Essential for the
    learning loop: trade → log → reflect → adjust.

    Args:
        symbol (Optional[str]): Filter by trading symbol (e.g. "EURUSD").
            None includes all symbols. (default: None)
        outcome (Optional[str]): Filter by outcome — "win", "loss",
            "breakeven", "open". (default: None)
        regime (Optional[str]): Filter by market regime at decision time —
            e.g. "trending_up", "ranging", "volatile". (default: None)
        emotional_self_report (Optional[str]): Filter by emotional state —
            e.g. "confident", "hesitant", "uncertain". (default: None)
        mistake_category (Optional[str]): Filter by mistake category —
            e.g. "fomo", "revenge_trading", "ignored_sl", "bad_rr",
            "overtrading". (default: None)
        action (Optional[str]): Filter by action type — "entry", "exit",
            "modify_sl", etc. (default: None)
        limit (int): Maximum number of decisions to return (default: 20,
            range: 1-1000). Higher values for comprehensive reviews.

    Returns:
        dict: Query result with keys:
            - count (int): Number of decisions returned
            - decisions (list): Array of decision dicts (structure matches
              mt5_log_trade_decision parameters with stored timestamps)
            - warning (str, present on failure): "Journal unavailable"
            - error (str, present on failure): Error description

    Example: mt5_reflect_on_trades(outcome="loss", limit=10)

    Note: This is a READ-ONLY query tool. It does not modify journal entries.
    Use mt5_log_trade_decision with a decision_id to update records. Results
    depend on thorough logging — sparse journals yield sparse reflections.
    """
    try:
        from mt5_mcp.services.trade_journal_db import get_journal_db

        journal = get_journal_db()
    except Exception:
        return {"count": 0, "decisions": [], "warning": "Journal unavailable"}

    try:
        decisions = journal.query(
            symbol=symbol,
            outcome=outcome,
            regime=regime,
            emotional_self_report=emotional_self_report,
            mistake_category=mistake_category,
            action=action,
            limit=limit,
        )
        return {"count": len(decisions), "decisions": decisions}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="mt5_trading_insights", annotations=_META_ANNOTATIONS)
def mt5_trading_insights(
    symbol: Optional[str] = None,
    days: int = 7,
) -> dict:
    """Get aggregated trading insights and performance summary from the journal.

    Use this for AI agent meta-analysis — view win/loss ratios by regime,
    emotional state patterns, common mistake categories, and overall trading
    quality trends over a lookback period. Designed to help the agent
    calibrate its decision-making based on historical results.

    Args:
        symbol (Optional[str]): Filter insights to a specific symbol (e.g.
            "EURUSD"). None includes all symbols. (default: None)
        days (int): Lookback period in days (default: 7, range: 1-365).
            Longer periods give more statistically significant insights.

    Returns:
        dict: Aggregated insights with keys:
            - total_decisions (int): Total decisions in lookback period
            - win_rate (float): Win rate as decimal 0.0-1.0
            - avg_confidence (float): Average confidence level across decisions
            - by_outcome (dict): Breakdown of decisions by outcome category
            - by_regime (dict): Performance breakdown by market regime
            - by_emotional_state (dict): Performance by emotional state
            - by_mistake_category (dict): Mistake frequency and impact
            - by_symbol (dict): Per-symbol decision breakdown
            - quality_trend (dict, optional): Quality rating trend over time
            - quality_rating_avg (float, optional): Average quality rating
            - top_lessons (list, optional): Most frequently cited lessons
            - filtered_symbol (str, present if symbol filter applied): The
              filtered symbol
            - symbol_decision_count (int, present if symbol filter applied):
              Number of decisions for the filtered symbol
            - error (str, present on failure): Error description

    Example: mt5_trading_insights(days=30)

    Note: Insights are derived solely from journal entries logged via
    mt5_log_trade_decision. Empty or sparse journals yield minimal insights.
    The quality of insights depends on the completeness of logged parameters
    (especially regime, emotional_self_report, and outcome).
    """
    try:
        from mt5_mcp.services.trade_journal_db import get_journal_db

        journal = get_journal_db()
    except Exception:
        return {"error": "Journal unavailable"}

    try:
        insights = journal.get_reflection_insights(lookback_days=days)
        if symbol:
            all_decisions = journal.query(symbol=symbol, limit=1000)
            insights["filtered_symbol"] = symbol
            insights["symbol_decision_count"] = len(all_decisions)
        return insights
    except Exception as e:
        return {"error": str(e)}
