"""SQLite database for trade history, position tracking, and daily snapshots.

This module provides persistence for the autonomous trading daemon.
The database lives at ``~/.tradingagents/trades.db`` by default and is
created automatically on first use.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.getenv(
    "TRADINGAGENTS_DB_PATH",
    os.path.join(os.path.expanduser("~"), ".tradingagents", "trades.db"),
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
-- Orders placed through Alpaca
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,              -- Alpaca order ID
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                -- BUY / SELL
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,          -- MARKET / LIMIT / STOP / BRACKET
    status TEXT NOT NULL,              -- SUBMITTED / FILLED / CANCELLED / EXPIRED
    submitted_at TEXT,
    filled_at TEXT,
    filled_price REAL,
    signal TEXT,                       -- Buy / Overweight / Hold / Underweight / Sell
    pipeline_run_id TEXT,
    guardrail_result TEXT,            -- APPROVED / BLOCKED:reason
    entry_orl REAL,                   -- Opening Range Low (initial stop)
    entry_lod REAL,                   -- Low of Day on entry (set after Day 1 close)
    pipeline_mode TEXT DEFAULT 'full', -- 'full' (LLM) or 'quant' for A/B tracking
    notes TEXT
);

-- Tracked positions (our state, not Alpaca's)
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,        -- Actual fill price once confirmed (signal price while PENDING)
    entry_orl REAL NOT NULL,          -- IMMUTABLE initial stop (consolidation pivot floor;
                                      -- "ORL" is a legacy name from the old opening-range entry)
    entry_lod REAL,                   -- Day 1 LOD (set after Day 1 close)
    current_stop REAL,                -- Current protective stop (updated by stop raises)
    current_qty REAL NOT NULL,
    original_qty REAL NOT NULL,
    day_count INTEGER DEFAULT 1,      -- Trading days held
    trimmed INTEGER DEFAULT 0,        -- Has 50% been sold? (0/1)
    trim_date TEXT,
    breakeven_stop_active INTEGER DEFAULT 0,
    trailing_stop_active INTEGER DEFAULT 0,
    entry_order_id TEXT,              -- Alpaca entry order ID (for fill reconciliation)
    stop_order_id TEXT,               -- Current Alpaca stop order ID
    status TEXT DEFAULT 'PENDING',    -- PENDING (order submitted) / OPEN (filled) / CLOSED / CANCELLED (never filled)
    closed_at TEXT,
    close_reason TEXT,                -- LOD_STOP / DAY1_RED / TRIM / TRAIL_10SMA / MANUAL / PARABOLIC / ENTRY_NEVER_FILLED
    pipeline_mode TEXT DEFAULT 'full', -- 'full' (LLM) or 'quant' for A/B tracking
    sector TEXT                        -- GICS-style sector, captured at entry for the concentration guardrail
);

-- Daily portfolio snapshots
CREATE TABLE IF NOT EXISTS daily_snapshots (
    date TEXT PRIMARY KEY,
    portfolio_value REAL,
    cash REAL,
    invested REAL,
    daily_pnl REAL,
    daily_pnl_pct REAL,
    positions_count INTEGER,
    trades_executed INTEGER DEFAULT 0,
    trades_blocked INTEGER DEFAULT 0
);

-- Screening results log
CREATE TABLE IF NOT EXISTS screening_log (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT,                       -- alpaca_screener / watchlist / finnhub
    score REAL,
    selected_for_pipeline INTEGER DEFAULT 0,
    signal_result TEXT,               -- Buy / Hold / Sell (filled after pipeline)
    PRIMARY KEY (date, symbol)
);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class TradeDB:
    """SQLite-backed trade persistence layer.

    Parameters
    ----------
    db_path : str, optional
        Path to the SQLite database file.  Created if it doesn't exist.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        """Create tables if they don't exist and run migrations."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Migrations for existing databases
            self._migrate(conn)
        logger.info("TradeDB initialised at %s", self.db_path)

    def _migrate(self, conn):
        """Add columns that may be missing in older databases."""
        migrations = [
            ("orders", "pipeline_mode", "TEXT DEFAULT 'full'"),
            ("positions", "pipeline_mode", "TEXT DEFAULT 'full'"),
            ("positions", "current_stop", "REAL"),
            ("positions", "entry_order_id", "TEXT"),
            ("positions", "sector", "TEXT"),
        ]
        for table, column, col_type in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                logger.info("Migration: added %s.%s", table, column)
            except sqlite3.OperationalError:
                pass  # column already exists

        # Backfill current_stop for legacy rows: entry_orl used to be
        # overwritten by every stop update, so it holds the last stop.
        conn.execute(
            "UPDATE positions SET current_stop = entry_orl "
            "WHERE current_stop IS NULL"
        )

    @contextmanager
    def _connect(self):
        """Yield a sqlite3 connection with row_factory set to Row."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- orders -------------------------------------------------------------

    def log_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        status: str,
        signal: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
        guardrail_result: Optional[str] = None,
        filled_price: Optional[float] = None,
        entry_orl: Optional[float] = None,
        entry_lod: Optional[float] = None,
        notes: Optional[str] = None,
    ):
        """Insert or update an order record."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders
                    (id, symbol, side, qty, order_type, status, submitted_at,
                     filled_price, signal, pipeline_run_id, guardrail_result,
                     entry_orl, entry_lod, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    filled_at = CASE WHEN excluded.status = 'FILLED'
                                     THEN datetime('now') ELSE filled_at END,
                    filled_price = COALESCE(excluded.filled_price, filled_price),
                    guardrail_result = COALESCE(excluded.guardrail_result, guardrail_result),
                    entry_lod = COALESCE(excluded.entry_lod, entry_lod),
                    notes = COALESCE(excluded.notes, notes)
                """,
                (
                    order_id,
                    symbol,
                    side,
                    qty,
                    order_type,
                    status,
                    datetime.now().isoformat(),
                    filled_price,
                    signal,
                    pipeline_run_id,
                    guardrail_result,
                    entry_orl,
                    entry_lod,
                    notes,
                ),
            )

    def get_orders_for_symbol(self, symbol: str) -> list[dict]:
        """Return all orders for *symbol*, most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE symbol = ? ORDER BY submitted_at DESC",
                (symbol,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_all_orders(
        self,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Return orders with optional filtering, most recent first."""
        query = "SELECT * FROM orders WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY submitted_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    # -- positions ----------------------------------------------------------

    def get_closed_positions(self, limit: int = 50) -> list[dict]:
        """Return closed positions, most recently closed first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'CLOSED' "
                "ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]


    def open_position(
        self,
        symbol: str,
        entry_date: str,
        entry_price: float,
        entry_orl: float,
        qty: float,
        stop_order_id: Optional[str] = None,
        entry_order_id: Optional[str] = None,
        pipeline_mode: str = "full",
        status: str = "PENDING",
        sector: Optional[str] = None,
    ):
        """Record a newly submitted entry.

        Positions start as ``PENDING`` (order submitted, not yet filled).
        ``mark_position_filled`` promotes them to ``OPEN`` with the actual
        fill price once Alpaca confirms;  ``cancel_pending_position``
        retires entries that never triggered.  *entry_price* is the
        intended signal price until the fill is confirmed.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions
                    (symbol, entry_date, entry_price, entry_orl, current_stop,
                     current_qty, original_qty, stop_order_id, entry_order_id,
                     pipeline_mode, status, sector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    entry_date = excluded.entry_date,
                    entry_price = excluded.entry_price,
                    entry_orl = excluded.entry_orl,
                    current_stop = excluded.current_stop,
                    entry_order_id = excluded.entry_order_id,
                    pipeline_mode = excluded.pipeline_mode,
                    status = excluded.status,
                    sector = COALESCE(excluded.sector, sector),
                    current_qty = excluded.current_qty,
                    original_qty = excluded.original_qty,
                    stop_order_id = COALESCE(excluded.stop_order_id, stop_order_id),
                    day_count = 1,
                    trimmed = 0,
                    closed_at = NULL,
                    close_reason = NULL,
                    breakeven_stop_active = 0,
                    trailing_stop_active = 0
                """,
                (
                    symbol, entry_date, entry_price, entry_orl, entry_orl,
                    qty, qty, stop_order_id, entry_order_id, pipeline_mode,
                    status, sector,
                ),
            )

    def get_open_positions(self, include_pending: bool = False) -> list[dict]:
        """Return positions we hold (status ``OPEN``).

        Parameters
        ----------
        include_pending : bool
            When True, also include ``PENDING`` positions (entry order
            submitted but not yet filled).  Use this for slot-counting
            (guardrails, already-held checks) so unfilled buy-stops
            still reserve a position slot.  Position management (exits,
            trims, day counts) must NOT include pending entries.
        """
        query = "SELECT * FROM positions WHERE status = 'OPEN'"
        if include_pending:
            query = "SELECT * FROM positions WHERE status IN ('OPEN', 'PENDING')"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
            return [dict(row) for row in rows]

    def get_pending_positions(self) -> list[dict]:
        """Return positions whose entry order has not been confirmed filled."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'PENDING'"
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_position_filled(
        self,
        symbol: str,
        fill_price: Optional[float] = None,
        fill_date: Optional[str] = None,
        fill_qty: Optional[float] = None,
    ):
        """Promote a PENDING position to OPEN with actual fill details.

        Called by fill reconciliation once Alpaca confirms the entry
        order filled.  Replaces the intended signal price with the real
        average fill price.
        """
        updates: dict = {"status": "OPEN"}
        if fill_price:
            updates["entry_price"] = fill_price
        if fill_date:
            updates["entry_date"] = fill_date
        if fill_qty:
            updates["current_qty"] = fill_qty
            updates["original_qty"] = fill_qty
        self.update_position(symbol, **updates)

    def cancel_pending_position(self, symbol: str, reason: str = "ENTRY_NEVER_FILLED"):
        """Retire a PENDING position whose entry order never filled.

        Uses status ``CANCELLED`` (not ``CLOSED``) so these rows are
        excluded from trade history / P&L queries.
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE positions
                SET status = 'CANCELLED', closed_at = ?, close_reason = ?
                WHERE symbol = ? AND status = 'PENDING'
                """,
                (datetime.now().isoformat(), reason, symbol),
            )

    def get_position(self, symbol: str) -> Optional[dict]:
        """Return the position record for *symbol*, or ``None``."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE symbol = ?", (symbol,)
            ).fetchone()
            return dict(row) if row else None

    def update_position(self, symbol: str, **kwargs):
        """Update arbitrary fields on the position for *symbol*.

        Example::

            db.update_position("NVDA", day_count=3, entry_lod=122.90)
        """
        if not kwargs:
            return
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [symbol]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE positions SET {set_clause} WHERE symbol = ?",
                values,
            )

    def close_position(self, symbol: str, reason: str):
        """Mark a position as closed."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE positions
                SET status = 'CLOSED',
                    closed_at = ?,
                    close_reason = ?
                WHERE symbol = ?
                """,
                (datetime.now().isoformat(), reason, symbol),
            )

    # -- daily snapshots ----------------------------------------------------

    def save_daily_snapshot(
        self,
        date: str,
        portfolio_value: float,
        cash: float,
        invested: float,
        daily_pnl: float,
        daily_pnl_pct: float,
        positions_count: int,
        trades_executed: int = 0,
        trades_blocked: int = 0,
    ):
        """Insert or replace the daily snapshot."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_snapshots
                    (date, portfolio_value, cash, invested, daily_pnl,
                     daily_pnl_pct, positions_count, trades_executed, trades_blocked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    portfolio_value = excluded.portfolio_value,
                    cash = excluded.cash,
                    invested = excluded.invested,
                    daily_pnl = excluded.daily_pnl,
                    daily_pnl_pct = excluded.daily_pnl_pct,
                    positions_count = excluded.positions_count,
                    trades_executed = excluded.trades_executed,
                    trades_blocked = excluded.trades_blocked
                """,
                (
                    date,
                    portfolio_value,
                    cash,
                    invested,
                    daily_pnl,
                    daily_pnl_pct,
                    positions_count,
                    trades_executed,
                    trades_blocked,
                ),
            )

    def get_daily_snapshot(self, date: str) -> Optional[dict]:
        """Return the snapshot for a given date, or ``None``."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_snapshots WHERE date = ?", (date,)
            ).fetchone()
            return dict(row) if row else None

    def get_recent_snapshots(self, days: int = 30) -> list[dict]:
        """Return the most recent *days* snapshots."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
            return [dict(row) for row in rows]

    # -- screening log ------------------------------------------------------

    def log_screening_result(
        self,
        date: str,
        symbol: str,
        source: str,
        score: float,
        selected_for_pipeline: bool = False,
        signal_result: Optional[str] = None,
    ):
        """Insert or update a screening log entry."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO screening_log
                    (date, symbol, source, score, selected_for_pipeline, signal_result)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                    source = excluded.source,
                    score = excluded.score,
                    selected_for_pipeline = excluded.selected_for_pipeline,
                    signal_result = COALESCE(excluded.signal_result, signal_result)
                """,
                (
                    date,
                    symbol,
                    source,
                    score,
                    int(selected_for_pipeline),
                    signal_result,
                ),
            )

    def get_screening_results(self, date: str) -> list[dict]:
        """Return all screening results for a given date."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM screening_log WHERE date = ? ORDER BY score DESC",
                (date,),
            ).fetchall()
            return [dict(row) for row in rows]

    # -- convenience methods for daemon workflow ----------------------------

    def log_screening(
        self,
        date: str,
        screened: int,
        passed: int,
        symbols: list,
        regime: str = "",
    ):
        """Log a batch screening result (convenience wrapper)."""
        for symbol in symbols:
            self.log_screening_result(
                date=date,
                symbol=symbol,
                source="hybrid_screener",
                score=1.0,
                selected_for_pipeline=True,
            )

    def record_order(self, symbol: str, side: str, qty: int, order_type: str,
                     limit_price: float = None, stop_price: float = None,
                     order_id: str = "", status: str = "submitted", **kwargs):
        """Record an order (convenience wrapper for log_order)."""
        self.log_order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            status=status,
            entry_orl=stop_price,
            notes=f"limit={limit_price}" if limit_price else None,
        )

    def record_snapshot(self, date: str, portfolio_value: float, cash: float,
                        num_positions: int, entries_today: int = 0,
                        exits_today: int = 0, regime: str = "", **kwargs):
        """Record a daily snapshot (convenience wrapper).

        Computes actual daily P&L by comparing to the previous snapshot.
        """
        invested = portfolio_value - cash

        # Compute daily P&L from previous snapshot
        daily_pnl = 0.0
        daily_pnl_pct = 0.0
        try:
            recent = self.get_recent_snapshots(days=2)
            # Find the most recent snapshot that is NOT today's date
            prev = next(
                (s for s in recent if s.get("date") != date),
                None,
            )
            if prev:
                prev_value = prev.get("portfolio_value", portfolio_value)
                if prev_value > 0:
                    daily_pnl = portfolio_value - prev_value
                    daily_pnl_pct = daily_pnl / prev_value
        except Exception as exc:
            logger.warning("Could not compute daily P&L: %s", exc)

        self.save_daily_snapshot(
            date=date,
            portfolio_value=portfolio_value,
            cash=cash,
            invested=invested,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            positions_count=num_positions,
            trades_executed=entries_today + exits_today,
        )

    def increment_day_count(self, symbol: str):
        """Increment the day counter for a position."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE positions SET day_count = day_count + 1 WHERE symbol = ? AND status = 'OPEN'",
                (symbol,),
            )

    def mark_trimmed(self, symbol: str):
        """Mark a position as having been trimmed."""
        self.update_position(
            symbol,
            trimmed=1,
            trim_date=datetime.now().isoformat(),
        )

    def update_stop(self, symbol: str, new_stop: float, stop_type: str = ""):
        """Update the tracked CURRENT stop price for a position.

        ``entry_orl`` (the immutable initial stop) is deliberately left
        untouched — only ``current_stop`` moves as stops are raised.

        Parameters
        ----------
        stop_type : str, optional
            ``"breakeven"``, ``"trailing"``, or ``"lod"`` — sets the
            corresponding flag column for audit.
        """
        updates = {"current_stop": new_stop}
        if stop_type == "breakeven":
            updates["breakeven_stop_active"] = 1
            updates["trailing_stop_active"] = 0
        elif stop_type == "trailing":
            updates["breakeven_stop_active"] = 0
            updates["trailing_stop_active"] = 1
        self.update_position(symbol, **updates)

    def update_order_status(
        self,
        order_id: str,
        status: str,
        filled_price: Optional[float] = None,
    ):
        """Sync an order row's status from Alpaca (fill reconciliation)."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = ?,
                    filled_price = COALESCE(?, filled_price),
                    filled_at = CASE WHEN ? = 'FILLED' AND filled_at IS NULL
                                     THEN datetime('now') ELSE filled_at END
                WHERE id = ?
                """,
                (status, filled_price, status, order_id),
            )

