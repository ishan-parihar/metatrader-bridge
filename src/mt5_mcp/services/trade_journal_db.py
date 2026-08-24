"""SQLite-backed Trade Journal with AI reasoning capture.

Every trading decision is logged with:
- Market context (regime, ATR, indicators)
- AI reasoning (justification, confidence, alternatives considered)
- Emotional self-report
- Post-trade reflection (lesson learned, mistake category, quality rating)

This enables agentic metacognition — the AI can query its own history
to identify patterns: "When I'm anxious, I exit too early."
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .mistake_categories import MistakeCategory

_MistakeCategoryInput = Optional[Union[MistakeCategory, str]]


def _normalize_mistake_category(value: _MistakeCategoryInput) -> Optional[str]:
    if value is None:
        return None
    return value.value if isinstance(value, MistakeCategory) else value


class TradeJournalDB:
    """SQLite-backed journal for trade decisions with full AI reasoning."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS trade_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id TEXT UNIQUE NOT NULL,
        timestamp TEXT NOT NULL,
        session_id TEXT,
        strategy_id TEXT,
        intent_id TEXT,

        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        action TEXT NOT NULL,
        entry_price REAL,
        exit_price REAL,
        sl REAL,
        tp REAL,
        volume_lots REAL,
        pnl REAL,
        duration_seconds REAL,

        -- Market context at decision time
        regime TEXT,
        atr_value REAL,
        atr_percent_of_price REAL,
        rsi_value REAL,
        ema_fast REAL,
        ema_slow REAL,
        indicator_snapshot TEXT,
        current_volatility_state TEXT,

        -- AI reasoning (metacognition layer)
        model_justification TEXT,
        indicators_considered TEXT,
        confidence_level REAL,
        risk_assessment TEXT,
        emotional_self_report TEXT,
        alternatives_considered TEXT,
        expected_duration TEXT,
        expected_move_points REAL,

        -- Post-trade reflection
        outcome TEXT,
        lesson_learned TEXT,
        would_do_differently TEXT,
        mistake_category TEXT,
        quality_rating INTEGER,
        note TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_symbol ON trade_decisions(symbol);
    CREATE INDEX IF NOT EXISTS idx_outcome ON trade_decisions(outcome);
    CREATE INDEX IF NOT EXISTS idx_regime ON trade_decisions(regime);
    CREATE INDEX IF NOT EXISTS idx_emotional ON trade_decisions(emotional_self_report);
    CREATE INDEX IF NOT EXISTS idx_mistake ON trade_decisions(mistake_category);
    CREATE INDEX IF NOT EXISTS idx_timestamp ON trade_decisions(timestamp);
    CREATE INDEX IF NOT EXISTS idx_intent_id ON trade_decisions(intent_id);
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = str(Path.home() / ".MetaTraderBridge" / "trading_journal.db")
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

        # Idempotent migration: add intent_id column if it doesn't exist
        try:
            self._conn.execute("ALTER TABLE trade_decisions ADD COLUMN intent_id TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_intent_id ON trade_decisions(intent_id)"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Idempotent migration: add note column if it doesn't exist
        try:
            self._conn.execute("ALTER TABLE trade_decisions ADD COLUMN note TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def log_decision(
        self,
        symbol: str,
        side: str,
        action: str,
        *,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        volume_lots: Optional[float] = None,
        pnl: Optional[float] = None,
        duration_seconds: Optional[float] = None,
        session_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        # Market context
        regime: Optional[str] = None,
        atr_value: Optional[float] = None,
        atr_percent_of_price: Optional[float] = None,
        rsi_value: Optional[float] = None,
        ema_fast: Optional[float] = None,
        ema_slow: Optional[float] = None,
        indicator_snapshot: Optional[dict] = None,
        current_volatility_state: Optional[str] = None,
        # AI reasoning
        model_justification: Optional[str] = None,
        indicators_considered: Optional[list[str]] = None,
        confidence_level: Optional[float] = None,
        risk_assessment: Optional[str] = None,
        emotional_self_report: Optional[str] = None,
        alternatives_considered: Optional[str] = None,
        expected_duration: Optional[str] = None,
        expected_move_points: Optional[float] = None,
        # Post-trade
        outcome: Optional[str] = None,
        lesson_learned: Optional[str] = None,
        would_do_differently: Optional[str] = None,
        mistake_category: _MistakeCategoryInput = None,
        quality_rating: Optional[int] = None,
        decision_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> str:
        """Log a trading decision with full reasoning context.

        Returns the decision_id for future reference.
        """
        if decision_id is None:
            decision_id = f"dec_{uuid.uuid4().hex[:12]}"

        ts = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """
            INSERT INTO trade_decisions (
                decision_id, timestamp, session_id, strategy_id, intent_id,
                symbol, side, action,
                entry_price, exit_price, sl, tp, volume_lots, pnl, duration_seconds,
                regime, atr_value, atr_percent_of_price, rsi_value, ema_fast, ema_slow,
                indicator_snapshot, current_volatility_state,
                model_justification, indicators_considered, confidence_level,
                risk_assessment, emotional_self_report, alternatives_considered,
                expected_duration, expected_move_points,
                outcome, lesson_learned, would_do_differently, mistake_category, quality_rating, note
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                decision_id,
                ts,
                session_id,
                strategy_id,
                intent_id,
                symbol,
                side,
                action,
                entry_price,
                exit_price,
                sl,
                tp,
                volume_lots,
                pnl,
                duration_seconds,
                regime,
                atr_value,
                atr_percent_of_price,
                rsi_value,
                ema_fast,
                ema_slow,
                json.dumps(indicator_snapshot, default=str)
                if indicator_snapshot
                else None,
                current_volatility_state,
                model_justification,
                json.dumps(indicators_considered, default=str)
                if indicators_considered
                else None,
                confidence_level,
                risk_assessment,
                emotional_self_report,
                alternatives_considered,
                expected_duration,
                expected_move_points,
                outcome,
                lesson_learned,
                would_do_differently,
                _normalize_mistake_category(mistake_category),
                quality_rating,
                note,
            ),
        )
        self._conn.commit()
        return decision_id

    def update_decision(
        self,
        decision_id: str,
        **kwargs,
    ) -> bool:
        """Update an existing decision record (e.g., add exit info to an entry)."""
        allowed_fields = {
            "exit_price",
            "pnl",
            "duration_seconds",
            "outcome",
            "lesson_learned",
            "would_do_differently",
            "mistake_category",
            "quality_rating",
            "emotional_self_report",
            "model_justification",
            "intent_id",
            "session_id",
            "strategy_id",
            "note",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not filtered:
            return False

        sets = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [decision_id]
        self._conn.execute(
            f"UPDATE trade_decisions SET {sets} WHERE decision_id = ?", values
        )
        self._conn.commit()
        return True

    def update_decision_outcome(
        self,
        decision_id: str,
        *,
        outcome: Optional[str] = None,
        exit_price: Optional[float] = None,
        pnl: Optional[float] = None,
        duration_seconds: Optional[float] = None,
        mistake_category: _MistakeCategoryInput = None,
    ) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE trade_decisions
            SET outcome = COALESCE(?, outcome),
                exit_price = COALESCE(?, exit_price),
                pnl = COALESCE(?, pnl),
                duration_seconds = COALESCE(?, duration_seconds),
                mistake_category = COALESCE(?, mistake_category)
            WHERE decision_id = ?
            """,
            (
                outcome,
                exit_price,
                pnl,
                duration_seconds,
                _normalize_mistake_category(mistake_category),
                decision_id,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def log_execution_result(
        self,
        symbol: str,
        side: str,
        action: str,
        *,
        intent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        volume_lots: Optional[float] = None,
        pnl: Optional[float] = None,
        outcome: Optional[str] = None,
        message: Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> str:
        if decision_id is None:
            decision_id = f"dec_{uuid.uuid4().hex[:12]}"

        ts = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """
            INSERT INTO trade_decisions (
                decision_id, timestamp, session_id, strategy_id, intent_id,
                symbol, side, action,
                entry_price, exit_price, sl, tp, volume_lots, pnl,
                outcome, model_justification
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                ts,
                session_id,
                strategy_id,
                intent_id,
                symbol,
                side,
                action,
                entry_price,
                exit_price,
                sl,
                tp,
                volume_lots,
                pnl,
                outcome,
                message,
            ),
        )
        self._conn.commit()
        return decision_id

    def query(
        self,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        regime: Optional[str] = None,
        emotional_self_report: Optional[str] = None,
        mistake_category: _MistakeCategoryInput = None,
        session_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        min_confidence: Optional[float] = None,
        limit: int = 50,
        order_by: str = "timestamp DESC",
    ) -> list[dict]:
        """Query trade decisions with filters."""
        conditions = []
        params = []

        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if side:
            conditions.append("side = ?")
            params.append(side)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if outcome:
            conditions.append("outcome = ?")
            params.append(outcome)
        if regime:
            conditions.append("regime = ?")
            params.append(regime)
        if emotional_self_report:
            conditions.append("emotional_self_report = ?")
            params.append(emotional_self_report)
        if mistake_category:
            conditions.append("mistake_category = ?")
            params.append(_normalize_mistake_category(mistake_category))
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if strategy_id:
            conditions.append("strategy_id = ?")
            params.append(strategy_id)
        if intent_id:
            conditions.append("intent_id = ?")
            params.append(intent_id)
        if min_confidence is not None:
            conditions.append("confidence_level >= ?")
            params.append(min_confidence)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM trade_decisions {where} ORDER BY {order_by} LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_reflection_insights(self, lookback_days: int = 7) -> dict:
        """Generate metacognitive insights for the AI to learn from.

        Returns patterns like:
        - "You tend to exit winners early when emotional_state=anxious"
        - "Your win rate is 70% in ranging regime but 30% in trending"
        - "Most common mistake: premature_exit (4 occurrences)"
        """
        since = datetime.now(timezone.utc)
        from datetime import timedelta

        cutoff = (since - timedelta(days=lookback_days)).isoformat()

        insights: dict = {}

        # Win rate by emotional state
        rows = self._conn.execute(
            """
            SELECT emotional_self_report,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins
            FROM trade_decisions
            WHERE timestamp > ? AND outcome IS NOT NULL AND outcome != 'still_open'
              AND emotional_self_report IS NOT NULL
            GROUP BY emotional_self_report
            """,
            (cutoff,),
        ).fetchall()

        if rows:
            insights["win_rate_by_emotional_state"] = {
                r["emotional_self_report"]: {
                    "total": r["total"],
                    "wins": r["wins"],
                    "win_rate": round(r["wins"] / r["total"] * 100, 1)
                    if r["total"] > 0
                    else 0,
                }
                for r in rows
            }

        # Win rate by regime
        rows = self._conn.execute(
            """
            SELECT regime,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins
            FROM trade_decisions
            WHERE timestamp > ? AND outcome IS NOT NULL AND outcome != 'still_open'
              AND regime IS NOT NULL
            GROUP BY regime
            """,
            (cutoff,),
        ).fetchall()

        if rows:
            insights["win_rate_by_regime"] = {
                r["regime"]: {
                    "total": r["total"],
                    "wins": r["wins"],
                    "win_rate": round(r["wins"] / r["total"] * 100, 1)
                    if r["total"] > 0
                    else 0,
                }
                for r in rows
            }

        # Mistake frequency
        rows = self._conn.execute(
            """
            SELECT mistake_category, COUNT(*) as count
            FROM trade_decisions
            WHERE timestamp > ? AND mistake_category IS NOT NULL
            GROUP BY mistake_category
            ORDER BY count DESC
            """,
            (cutoff,),
        ).fetchall()

        if rows:
            insights["mistake_frequency"] = [
                {"category": r["mistake_category"], "count": r["count"]} for r in rows
            ]

        # Average confidence on wins vs losses
        rows = self._conn.execute(
            """
            SELECT outcome,
                   AVG(confidence_level) as avg_confidence,
                   COUNT(*) as count
            FROM trade_decisions
            WHERE timestamp > ? AND confidence_level IS NOT NULL
              AND outcome IS NOT NULL AND outcome != 'still_open'
            GROUP BY outcome
            """,
            (cutoff,),
        ).fetchall()

        if rows:
            insights["confidence_by_outcome"] = {
                r["outcome"]: {
                    "avg_confidence": round(r["avg_confidence"], 2)
                    if r["avg_confidence"]
                    else None,
                    "count": r["count"],
                }
                for r in rows
            }

        # Recent lessons learned
        rows = self._conn.execute(
            """
            SELECT lesson_learned, symbol, outcome, timestamp
            FROM trade_decisions
            WHERE timestamp > ? AND lesson_learned IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 10
            """,
            (cutoff,),
        ).fetchall()

        if rows:
            insights["recent_lessons"] = [
                {
                    "lesson": r["lesson_learned"],
                    "symbol": r["symbol"],
                    "outcome": r["outcome"],
                }
                for r in rows
            ]

        # Overall stats
        row = self._conn.execute(
            """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                   AVG(pnl) as avg_pnl,
                   SUM(pnl) as total_pnl
            FROM trade_decisions
            WHERE timestamp > ? AND outcome IS NOT NULL AND outcome != 'still_open'
            """,
            (cutoff,),
        ).fetchone()

        if row:
            total = row["total"] or 0
            insights["overall"] = {
                "total_decisions": total,
                "wins": row["wins"] or 0,
                "losses": row["losses"] or 0,
                "win_rate": round((row["wins"] or 0) / total * 100, 1)
                if total > 0
                else 0,
                "avg_pnl": round(row["avg_pnl"], 2) if row["avg_pnl"] else 0,
                "total_pnl": round(row["total_pnl"], 2) if row["total_pnl"] else 0,
            }

        # Win rate by strategy
        rows = self._conn.execute(
            """
            SELECT strategy_id,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins
            FROM trade_decisions
            WHERE timestamp > ? AND outcome IS NOT NULL AND outcome != 'still_open'
              AND strategy_id IS NOT NULL
            GROUP BY strategy_id
            """,
            (cutoff,),
        ).fetchall()

        if rows:
            insights["win_rate_by_strategy"] = {
                r["strategy_id"]: {
                    "total": r["total"],
                    "wins": r["wins"],
                    "win_rate": round(r["wins"] / r["total"] * 100, 1)
                    if r["total"] > 0
                    else 0,
                }
                for r in rows
            }

        return insights

    def get_mistake_taxonomy(
        self, lookback_days: Optional[int] = None
    ) -> dict[str, int]:
        from datetime import timedelta

        taxonomy: dict[str, int] = {}
        if lookback_days is not None:
            since = datetime.now(timezone.utc)
            cutoff = (since - timedelta(days=lookback_days)).isoformat()
            rows = self._conn.execute(
                """
                SELECT mistake_category, COUNT(*) as count
                FROM trade_decisions
                WHERE timestamp > ? AND mistake_category IS NOT NULL
                GROUP BY mistake_category
                ORDER BY count DESC
                """,
                (cutoff,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT mistake_category, COUNT(*) as count
                FROM trade_decisions
                WHERE mistake_category IS NOT NULL
                GROUP BY mistake_category
                ORDER BY count DESC
                """
            ).fetchall()

        for r in rows:
            taxonomy[r["mistake_category"]] = r["count"]

        return taxonomy

    def get_decision(self, decision_id: str) -> Optional[dict]:
        """Get a single decision by ID."""
        row = self._conn.execute(
            "SELECT * FROM trade_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()


# Global singleton
_journal_db: Optional[TradeJournalDB] = None


def get_journal_db(db_path: Optional[str] = None) -> TradeJournalDB:
    """Get or create the global SQLite trade journal."""
    global _journal_db
    if _journal_db is None:
        _journal_db = TradeJournalDB(db_path=db_path)
    else:
        # Test connection liveness — recreate if dead
        try:
            _journal_db._conn.execute("SELECT 1")
        except Exception:
            _journal_db = TradeJournalDB(db_path=db_path)
    return _journal_db
