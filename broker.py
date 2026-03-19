"""Alpaca trading interface for options — optimized for speed."""

from __future__ import annotations

import math
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.trading.requests import MarketOrderRequest

import config
from logger_setup import setup_logger
from parser import Signal, parse_sell_size

logger = setup_logger("optionsbot.broker")


class AlpacaBroker:
    """Interface to Alpaca paper trading for options orders."""

    def __init__(self) -> None:
        self._client: Optional[TradingClient] = None
        self._buying_power: float = 0.0

    def connect(self) -> None:
        """Initialize the Alpaca trading client."""
        if not config.ALPACA_API_KEY or config.ALPACA_API_KEY.startswith("your_"):
            logger.warning(
                "Alpaca API key not configured — orders will fail. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
            )

        self._client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=True,
            url_override=config.ALPACA_BASE_URL,
        )
        logger.info("Connected to Alpaca (paper trading)")
        self.refresh_buying_power()

    @property
    def client(self) -> TradingClient:
        if self._client is None:
            raise RuntimeError("Broker not connected. Call connect() first.")
        return self._client

    def refresh_buying_power(self) -> float:
        """Fetch and cache current buying power."""
        try:
            account = self.client.get_account()
            self._buying_power = float(account.buying_power)
            logger.info("Buying power: $%.2f", self._buying_power)
        except Exception as exc:
            logger.error("Failed to refresh buying power: %s", exc)
        return self._buying_power

    def check_buying_power(self, estimated_cost: float) -> bool:
        """Check if we have enough buying power for a trade.

        Args:
            estimated_cost: Estimated cost in dollars (price * qty * 100).

        Returns:
            True if sufficient buying power.
        """
        if self._buying_power < config.MIN_BUYING_POWER:
            logger.warning(
                "Buying power $%.2f below minimum threshold $%.2f",
                self._buying_power, config.MIN_BUYING_POWER,
            )
            return False

        if estimated_cost > self._buying_power:
            logger.warning(
                "Insufficient buying power: need $%.2f, have $%.2f",
                estimated_cost, self._buying_power,
            )
            return False

        return True

    def buy_option(self, signal: Signal, quantity: int) -> dict:
        """Place a MARKET buy order for an options contract.

        Args:
            signal: Parsed BUY signal.
            quantity: Number of contracts.

        Returns:
            Dict with order details.
        """
        # Enforce max position size
        qty = min(quantity, config.MAX_POSITION_SIZE)
        if qty != quantity:
            logger.info("Capped quantity from %d to %d (MAX_POSITION_SIZE)", quantity, qty)

        occ_symbol = signal.occ_symbol

        # Check buying power (estimate: signal price * qty * 100)
        estimated_cost = signal.price * qty * 100
        if not self.check_buying_power(estimated_cost):
            raise ValueError(
                f"Insufficient buying power for {qty}x {occ_symbol} "
                f"(est. ${estimated_cost:.2f})"
            )

        logger.info("Placing MARKET BUY: %d x %s", qty, occ_symbol)

        try:
            order_request = MarketOrderRequest(
                symbol=occ_symbol,
                qty=qty,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            order = self.client.submit_order(order_data=order_request)

            result = {
                "order_id": str(order.id),
                "symbol": occ_symbol,
                "side": "BUY",
                "qty": qty,
                "type": "MARKET",
                "status": str(order.status),
            }
            logger.info("BUY order submitted: %s", result)

            # Refresh buying power after trade
            self.refresh_buying_power()

            return result

        except Exception as exc:
            logger.error("Failed to submit BUY order for %s: %s", occ_symbol, exc)
            raise

    def sell_option(
        self,
        signal: Signal,
        current_quantity: int,
    ) -> dict:
        """Place a MARKET sell order for an options position.

        Args:
            signal: Parsed SELL signal.
            current_quantity: Current number of contracts held.

        Returns:
            Dict with order details.
        """
        sell_fraction = parse_sell_size(signal.size) if signal.size else 1.0
        if sell_fraction is None:
            logger.warning("Could not parse sell size %r, defaulting to ALL OUT", signal.size)
            sell_fraction = 1.0

        sell_qty = self._calculate_sell_quantity(current_quantity, sell_fraction)
        occ_symbol = signal.occ_symbol

        logger.info(
            "Placing MARKET SELL: %d x %s (%.0f%% of %d)",
            sell_qty, occ_symbol, sell_fraction * 100, current_quantity,
        )

        try:
            order_request = MarketOrderRequest(
                symbol=occ_symbol,
                qty=sell_qty,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            order = self.client.submit_order(order_data=order_request)

            result = {
                "order_id": str(order.id),
                "symbol": occ_symbol,
                "side": "SELL",
                "qty": sell_qty,
                "type": "MARKET",
                "status": str(order.status),
                "fraction": sell_fraction,
            }
            logger.info("SELL order submitted: %s", result)

            self.refresh_buying_power()
            return result

        except Exception as exc:
            logger.error("Failed to submit SELL order for %s: %s", occ_symbol, exc)
            raise

    @staticmethod
    def _calculate_sell_quantity(current_qty: int, fraction: float) -> int:
        """Calculate number of contracts to sell.

        Always sells at least 1 contract. Rounds up partial contracts.
        """
        if fraction >= 1.0:
            return current_qty

        raw = current_qty * fraction
        result = max(1, math.ceil(raw))
        return min(result, current_qty)

    def get_positions(self) -> list[dict]:
        """Fetch all open positions from Alpaca."""
        try:
            positions = self.client.get_all_positions()
            return [
                {
                    "symbol": pos.symbol,
                    "qty": pos.qty,
                    "side": pos.side,
                    "avg_entry_price": str(pos.avg_entry_price),
                    "current_price": str(pos.current_price),
                    "unrealized_pl": str(pos.unrealized_pl),
                }
                for pos in positions
            ]
        except Exception as exc:
            logger.error("Failed to fetch positions: %s", exc)
            raise

    def get_account(self) -> dict:
        """Fetch Alpaca account information."""
        try:
            account = self.client.get_account()
            return {
                "id": str(account.id),
                "status": str(account.status),
                "equity": str(account.equity),
                "buying_power": str(account.buying_power),
                "cash": str(account.cash),
                "portfolio_value": str(account.portfolio_value),
            }
        except Exception as exc:
            logger.error("Failed to fetch account info: %s", exc)
            raise
