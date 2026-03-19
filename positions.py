"""Position tracking with SQLite database."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from logger_setup import setup_logger
from parser import Signal

logger = setup_logger("optionsbot.positions")


class PositionTracker:
    """Tracks trades and open positions using a SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                ticker TEXT NOT NULL,
                expiry TEXT,
                strike REAL NOT NULL,
                option_type TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                signal_raw TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN'
            )
        """)
        conn.commit()
        logger.info("Database initialized at %s", self.db_path)

    def record_trade(
        self,
        signal: Signal,
        quantity: int,
        status: str = "OPEN",
    ) -> int:
        """Record a trade in the database.

        Args:
            signal: The parsed signal.
            quantity: Number of contracts.
            status: Trade status (OPEN, PARTIAL, CLOSED).

        Returns:
            The trade row ID.
        """
        conn = self._get_conn()
        expiry_str = signal.expiry.isoformat() if signal.expiry else None

        cursor = conn.execute(
            """
            INSERT INTO trades (timestamp, action, ticker, expiry, strike,
                                option_type, price, quantity, signal_raw, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                signal.action,
                signal.ticker,
                expiry_str,
                signal.strike,
                signal.option_type,
                signal.price,
                quantity,
                signal.raw,
                status,
            ),
        )
        conn.commit()
        trade_id = cursor.lastrowid
        logger.info(
            "Recorded trade #%d: %s %d x %s %.1f%s @ %.2f [%s]",
            trade_id,
            signal.action,
            quantity,
            signal.ticker,
            signal.strike,
            "C" if signal.option_type == "CALL" else "P",
            signal.price,
            status,
        )
        return trade_id  # type: ignore[return-value]

    def get_open_positions(self) -> list[dict]:
        """Return all open positions (status OPEN or PARTIAL).

        Returns:
            List of position dicts.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE action = 'BUY' AND status IN ('OPEN', 'PARTIAL')
            ORDER BY timestamp DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_position_for_signal(self, signal: Signal) -> Optional[dict]:
        """Find the open position matching a sell signal.

        Matches on ticker, strike, and option_type.

        Args:
            signal: A SELL signal to match against open positions.

        Returns:
            The matching position dict, or None.
        """
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM trades
            WHERE action = 'BUY'
              AND ticker = ?
              AND strike = ?
              AND option_type = ?
              AND status IN ('OPEN', 'PARTIAL')
            ORDER BY timestamp ASC
            LIMIT 1
            """,
            (signal.ticker, signal.strike, signal.option_type),
        ).fetchone()

        if row:
            return dict(row)
        return None

    def update_position_status(self, trade_id: int, status: str) -> None:
        """Update the status of a position.

        Args:
            trade_id: The trade row ID.
            status: New status (OPEN, PARTIAL, CLOSED).
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE trades SET status = ? WHERE id = ?",
            (status, trade_id),
        )
        conn.commit()
        logger.info("Updated trade #%d status to %s", trade_id, status)

    def update_position_quantity(self, trade_id: int, new_quantity: int) -> None:
        """Update the remaining quantity on an open position.

        Args:
            trade_id: The trade row ID.
            new_quantity: Updated contract count.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE trades SET quantity = ? WHERE id = ?",
            (new_quantity, trade_id),
        )
        conn.commit()
        logger.debug("Updated trade #%d quantity to %d", trade_id, new_quantity)

    def close_position(self, trade_id: int) -> None:
        """Mark a position as fully closed.

        Args:
            trade_id: The trade row ID.
        """
        self.update_position_status(trade_id, "CLOSED")

    def get_trade_history(self, limit: int = 50) -> list[dict]:
        """Return recent trade history.

        Args:
            limit: Maximum number of trades to return.

        Returns:
            List of trade dicts ordered newest first.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_weekly_pnl(self) -> float:
        """Calculate realized P&L from last week (Mon-Fri).

        Used by the risk manager to set the weekly risk budget.

        Returns:
            Total realized P&L from the previous trading week.
        """
        conn = self._get_conn()
        today = date.today()
        # Find last Monday
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        last_monday = this_monday - timedelta(days=7)
        last_friday = this_monday - timedelta(days=2)  # Saturday - 2 = Friday... adjust
        # Actually: last week = last_monday to last_friday (inclusive)
        last_friday = this_monday - timedelta(days=1)  # Sunday before this monday
        # Simpler: just get all trades from 7-14 days ago
        start = last_monday.isoformat()
        end = this_monday.isoformat()

        rows = conn.execute(
            """
            SELECT action, price, quantity FROM trades
            WHERE timestamp >= ? AND timestamp < ?
              AND status IN ('CLOSED', 'PARTIAL')
            ORDER BY timestamp ASC
            """,
            (start, end),
        ).fetchall()

        total_revenue = 0.0
        total_cost = 0.0
        for row in rows:
            value = row["price"] * row["quantity"] * 100
            if row["action"] == "SELL":
                total_revenue += value
            elif row["action"] == "BUY":
                total_cost += value

        pnl = total_revenue - total_cost
        logger.info("Last week P&L (from %s to %s): $%.2f", start, end, pnl)
        return pnl

    def calculate_pnl(self, ticker: str, strike: float, option_type: str) -> Optional[float]:
        """Calculate realized P&L for a closed position.

        Sums up (sell_price * sell_qty) - (buy_price * buy_qty) for all
        matching trades.

        Args:
            ticker: The underlying ticker.
            strike: The strike price.
            option_type: CALL or PUT.

        Returns:
            P&L in dollars (per contract = price * 100), or None if no data.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT action, price, quantity FROM trades
            WHERE ticker = ? AND strike = ? AND option_type = ?
            ORDER BY timestamp ASC
            """,
            (ticker, strike, option_type),
        ).fetchall()

        if not rows:
            return None

        total_cost = 0.0
        total_revenue = 0.0

        for row in rows:
            # Options are priced per share, 100 shares per contract
            value = row["price"] * row["quantity"] * 100
            if row["action"] == "BUY":
                total_cost += value
            elif row["action"] == "SELL":
                total_revenue += value

        return total_revenue - total_cost
