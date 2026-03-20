"""Risk management engine — enforces position sizing and exposure rules.

Rules:
- Max 5% of account equity per trade
- Max total exposure: 8% of account (hard cap 10%)
- Lottos/rollups: only use 35% of profits from the original trade
- No stop losses — position sizing IS the risk management
"""

from __future__ import annotations

import config
from logger_setup import setup_logger

logger = setup_logger("optionsbot.risk")

# Lotto/rollup profit fraction — only risk 30-40% of profits from original trade
LOTTO_PROFIT_FRACTION = 0.35


class RiskManager:
    """Calculates position sizes and enforces risk guardrails."""

    def __init__(self) -> None:
        self._account_equity: float = 0.0
        self._buying_power: float = 0.0
        self._current_exposure: float = 0.0

    def update_account(self, equity: float, buying_power: float) -> None:
        """Update cached account values."""
        self._account_equity = equity
        self._buying_power = buying_power
        logger.info("Account updated: equity=$%.2f, buying_power=$%.2f", equity, buying_power)

    def update_exposure(self, total_exposure: float) -> None:
        """Update the cached total exposure (sum of all open position costs)."""
        self._current_exposure = total_exposure

    def calculate_position_size(
        self,
        signal_price: float,
        is_lotto: bool = False,
        is_rollup: bool = False,
        original_trade_profit: float = 0.0,
    ) -> int:
        """Calculate number of contracts to buy.

        Args:
            signal_price: Price per contract from the signal.
            is_lotto: Whether this is a lotto play.
            is_rollup: Whether this is a roll up.
            original_trade_profit: Profit from the original trade (for lotto/rollup sizing).

        Returns:
            Number of contracts to buy (0 = skip the trade).
        """
        if self._account_equity <= 0:
            logger.error("Account equity is $0 — cannot size position")
            return 0

        contract_cost = signal_price * 100  # Options: price * 100 shares

        if contract_cost <= 0:
            logger.error("Invalid contract cost: $%.2f", contract_cost)
            return 0

        # Step 1: Max 5% of account per trade
        max_trade_value = self._account_equity * (config.MAX_TRADE_PCT / 100.0)

        # Step 2: Lotto/rollup override — cap at 35% of recent profits
        if is_lotto or is_rollup:
            if original_trade_profit > 0:
                lotto_budget = original_trade_profit * LOTTO_PROFIT_FRACTION
                max_trade_value = min(max_trade_value, lotto_budget)
                logger.info(
                    "Lotto/rollup: capping at $%.2f (%.0f%% of $%.2f profit)",
                    lotto_budget, LOTTO_PROFIT_FRACTION * 100, original_trade_profit,
                )
            else:
                # No profit data — use minimum sizing (1% of account)
                min_trade_value = self._account_equity * (config.MIN_TRADE_PCT / 100.0)
                max_trade_value = min(max_trade_value, min_trade_value)
                logger.info("Lotto/rollup with no profit data — using min size $%.2f", max_trade_value)

        # Step 3: Calculate contracts
        contracts = max(1, int(max_trade_value / contract_cost))

        # Step 4: Hard cap on contracts
        contracts = min(contracts, config.MAX_POSITION_SIZE)

        # Step 5: Check total exposure (8% soft / 10% hard cap)
        if not self._check_exposure(contracts * contract_cost):
            contracts = self._reduce_to_fit_exposure(contract_cost)
            if contracts == 0:
                logger.warning("Cannot fit trade within exposure limits — skipping")
                return 0

        trade_value = contracts * contract_cost
        logger.info(
            "Position size: %d contracts @ $%.2f = $%.2f (%.1f%% of account)",
            contracts, signal_price, trade_value,
            (trade_value / self._account_equity) * 100,
        )

        return contracts

    def _check_exposure(self, additional_cost: float) -> bool:
        """Check if adding this trade keeps us within max exposure."""
        new_total = self._current_exposure + additional_cost
        hard_cap = self._account_equity * (config.HARD_CAP_EXPOSURE_PCT / 100.0)

        if new_total > hard_cap:
            logger.warning(
                "HARD CAP: exposure $%.2f + $%.2f = $%.2f exceeds %.0f%% cap ($%.2f)",
                self._current_exposure, additional_cost, new_total,
                config.HARD_CAP_EXPOSURE_PCT, hard_cap,
            )
            return False

        max_exposure = self._account_equity * (config.MAX_EXPOSURE_PCT / 100.0)
        if new_total > max_exposure:
            logger.warning(
                "Exposure $%.2f + $%.2f = $%.2f exceeds %.0f%% target ($%.2f) — proceeding with caution",
                self._current_exposure, additional_cost, new_total,
                config.MAX_EXPOSURE_PCT, max_exposure,
            )

        return True

    def _reduce_to_fit_exposure(self, contract_cost: float) -> int:
        """Try to find a smaller quantity that fits within exposure limits."""
        hard_cap = self._account_equity * (config.HARD_CAP_EXPOSURE_PCT / 100.0)
        remaining = hard_cap - self._current_exposure

        if remaining <= 0:
            return 0

        return max(0, int(remaining / contract_cost))

    def get_status(self) -> dict:
        """Return current risk status for logging/display."""
        exposure_pct = (self._current_exposure / self._account_equity * 100) if self._account_equity > 0 else 0

        return {
            "account_equity": self._account_equity,
            "buying_power": self._buying_power,
            "current_exposure": self._current_exposure,
            "exposure_pct": f"{exposure_pct:.1f}%",
            "max_exposure_pct": f"{config.MAX_EXPOSURE_PCT}%",
        }
