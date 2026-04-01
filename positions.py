"""Position tracking with SQLite database."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Optional

from logger_setup import setup_logger
from parser import Signal

logger = setup_logger("optionsbot.positions")


def _position_key(ticker: str, strike: float, option_type: str) -> str:
    """Normalized key for position lookup: 'SPY|520.0|CALL'."""
    return f"{ticker.upper()}|{strike:.2f}|{option_type.upper()}"


class PositionTracker:
    """Tracks trades and open positions using a SQLite database.

    Maintains an in-memory hashmap of open positions for O(1) lookups.
    The DB is the source of truth; the map is rebuilt on startup.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        # In-memory position map: key -> position dict
        self._open_map: dict[str, dict] = {}
        self._init_db()
        self._rebuild_map()

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

    def _rebuild_map(self) -> None:
        """Rebuild in-memory position map from the database."""
        self._open_map.clear()
        for pos in self.get_open_positions():
            key = _position_key(pos["ticker"], pos["strike"], pos["option_type"])
            self._open_map[key] = pos
        logger.info("Position map rebuilt: %d open position(s)", len(self._open_map))

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

        # Keep in-memory map in sync
        if signal.action == "BUY" and status in ("OPEN", "PARTIAL"):
            key = _position_key(signal.ticker, signal.strike, signal.option_type)
            self._open_map[key] = {
                "id": trade_id,
                "ticker": signal.ticker,
                "strike": signal.strike,
                "option_type": signal.option_type,
                "expiry": signal.expiry.isoformat() if signal.expiry else None,
                "price": signal.price,
                "quantity": quantity,
                "status": status,
            }

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

        Uses in-memory hashmap for O(1) lookup. Falls back to fuzzy match
        on ticker + option_type if exact strike doesn't match (handles
        float rounding edge cases).

        Args:
            signal: A SELL signal to match against open positions.

        Returns:
            The matching position dict, or None.
        """
        # Fast path: exact hashmap lookup
        key = _position_key(signal.ticker, signal.strike, signal.option_type)
        pos = self._open_map.get(key)
        if pos:
            return pos

        # Fuzzy fallback: same ticker + option_type, closest strike within $1
        best = None
        best_diff = float("inf")
        for k, p in self._open_map.items():
            if p["ticker"] == signal.ticker and p["option_type"] == signal.option_type:
                diff = abs(p["strike"] - signal.strike)
                if diff < best_diff and diff <= 1.0:
                    best = p
                    best_diff = diff

        if best:
            logger.warning(
                "Fuzzy match for SELL %s %.1f%s — matched open position at strike %.1f (diff=%.2f)",
                signal.ticker, signal.strike, signal.option_type[0],
                best["strike"], best_diff,
            )
            return best

        # Last resort: check DB directly in case map is stale
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM trades
            WHERE action = 'BUY'
              AND ticker = ?
              AND option_type = ?
              AND status IN ('OPEN', 'PARTIAL')
              AND ABS(strike - ?) <= 1.0
            ORDER BY ABS(strike - ?) ASC
            LIMIT 1
            """,
            (signal.ticker, signal.option_type, signal.strike, signal.strike),
        ).fetchone()

        if row:
            result = dict(row)
            logger.warning(
                "Position found in DB but missing from map — resyncing. strike=%.1f",
                result["strike"],
            )
            self._rebuild_map()
            return result

        logger.warning(
            "No position found for SELL %s %.1f%s — open positions: %s",
            signal.ticker, signal.strike, signal.option_type[0],
            list(self._open_map.keys()) or "NONE",
        )
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

        # Update in-memory map
        for pos in self._open_map.values():
            if pos.get("id") == trade_id:
                pos["quantity"] = new_quantity
                break

    def close_position(self, trade_id: int) -> None:
        """Mark a position as fully closed.

        Args:
            trade_id: The trade row ID.
        """
        self.update_position_status(trade_id, "CLOSED")

        # Remove from in-memory map
        to_remove = None
        for key, pos in self._open_map.items():
            if pos.get("id") == trade_id:
                to_remove = key
                break
        if to_remove:
            del self._open_map[to_remove]

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

    def get_last_closed_profit(self, ticker: str) -> float:
        """Get realized P&L from the most recently closed position for a ticker.

        Used for lotto/rollup sizing: "I made $1000 on SPY, so my lotto
        budget is 35% of $1000 = $350."

        Finds the last BUY trade for this ticker that is CLOSED, then sums
        all SELL trades with the same ticker/strike/option_type to get total
        revenue, and computes profit = revenue - cost.

        Returns:
            Realized profit in dollars, or 0.0 if no closed position found.
        """
        conn = self._get_conn()

        # Find the most recent closed BUY for this ticker
        last_buy = conn.execute(
            """
            SELECT * FROM trades
            WHERE action = 'BUY' AND ticker = ? AND status = 'CLOSED'
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()

        if not last_buy:
            return 0.0

        # Find all SELL trades matching this position
        sells = conn.execute(
            """
            SELECT price, quantity FROM trades
            WHERE action = 'SELL'
              AND ticker = ?
              AND strike = ?
              AND option_type = ?
              AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (last_buy["ticker"], last_buy["strike"],
             last_buy["option_type"], last_buy["timestamp"]),
        ).fetchall()

        buy_cost = last_buy["price"] * last_buy["quantity"] * 100
        sell_revenue = sum(row["price"] * row["quantity"] * 100 for row in sells)
        profit = sell_revenue - buy_cost

        logger.info(
            "Last closed %s position: bought %d @ $%.2f ($%.2f), sold for $%.2f, profit=$%.2f",
            ticker, last_buy["quantity"], last_buy["price"], buy_cost, sell_revenue, profit,
        )

        return max(0.0, profit)

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
