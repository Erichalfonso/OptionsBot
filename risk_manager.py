"""Risk management engine — enforces position sizing and exposure rules.

Based on the Optionsful risk management philosophy:
- Position size: 1-5% of account per trade
- Max total exposure: 8% of account (hard cap 10%)
- Lottos/rollups: only use 30-40% of profits from the original trade
- Day-of-week decay: size down as expiration approaches
- Weekly risk budget: risk only previous week's profits, divided across days
- No stop losses by default — position sizing IS the risk management
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import config
from logger_setup import setup_logger

logger = setup_logger("optionsbot.risk")


# Day-of-week multipliers for position sizing (Mon=0 through Fri=4)
# Closer to weekly expiration = smaller size
DOW_MULTIPLIERS = {
    0: 1.00,   # Monday    — full size
    1: 0.75,   # Tuesday   — 75%
    2: 0.65,   # Wednesday — 65%
    3: 0.40,   # Thursday  — 40%
    4: 0.30,   # Friday    — 30%
}

# Lotto/rollup profit fraction — only risk 30-40% of profits from original trade
LOTTO_PROFIT_FRACTION = 0.35


class RiskManager:
    """Calculates position sizes and enforces risk guardrails."""

    def __init__(self) -> None:
        self._account_equity: float = 0.0
        self._buying_power: float = 0.0
        self._weekly_pnl: float = 0.0
        self._daily_risk_used: float = 0.0
        self._daily_risk_budget: float = 0.0
        self._current_day: Optional[date] = None

    def update_account(self, equity: float, buying_power: float) -> None:
        """Update cached account values."""
        self._account_equity = equity
        self._buying_power = buying_power
        logger.info("Account updated: equity=$%.2f, buying_power=$%.2f", equity, buying_power)

    def set_weekly_pnl(self, pnl: float) -> None:
        """Set last week's P&L for weekly risk budget calculation.

        The weekly risk budget divides last week's profits across 5 trading days,
        weighted toward the beginning of the week.
        """
        self._weekly_pnl = max(0.0, pnl)  # Only budget profits, not losses
        if self._weekly_pnl > 0:
            self._calculate_daily_budget()
            logger.info("Weekly P&L set to $%.2f — daily budget: $%.2f", pnl, self._daily_risk_budget)
        else:
            logger.info("No profits from last week — using percentage-based sizing only")

    def _calculate_daily_budget(self) -> None:
        """Calculate today's risk budget from weekly profits.

        Distribution: Mon $1500, Tue $1200, Wed $1000, Thu $800, Fri $600
        out of $5000 total = [30%, 24%, 20%, 16%, 10%]
        """
        day = datetime.now().weekday()
        daily_weights = {0: 0.30, 1: 0.24, 2: 0.20, 3: 0.16, 4: 0.10}
        weight = daily_weights.get(day, 0.20)
        self._daily_risk_budget = self._weekly_pnl * weight
        self._current_day = date.today()
        self._daily_risk_used = 0.0

    def reset_daily_if_needed(self) -> None:
        """Reset daily risk tracking if it's a new day."""
        today = date.today()
        if self._current_day != today:
            self._current_day = today
            self._daily_risk_used = 0.0
            if self._weekly_pnl > 0:
                self._calculate_daily_budget()
            logger.info("New trading day — daily risk reset")

    def get_max_trade_value(self) -> float:
        """Get the maximum dollar value for a single trade.

        Returns the smaller of:
        - 5% of account equity (max per-trade rule)
        - Remaining daily risk budget (if weekly budget is active)
        """
        max_pct = self._account_equity * (config.MAX_TRADE_PCT / 100.0)

        if self._daily_risk_budget > 0:
            remaining_budget = self._daily_risk_budget - self._daily_risk_used
            if remaining_budget <= 0:
                logger.warning("Daily risk budget exhausted ($%.2f used of $%.2f)",
                               self._daily_risk_used, self._daily_risk_budget)
                return 0.0
            return min(max_pct, remaining_budget)

        return max_pct

    def calculate_position_size(
        self,
        signal_price: float,
        expiry: Optional[date] = None,
        is_lotto: bool = False,
        is_rollup: bool = False,
        original_trade_profit: float = 0.0,
    ) -> int:
        """Calculate number of contracts to buy based on risk rules.

        Args:
            signal_price: Price per contract from the signal.
            expiry: Option expiration date (for day-of-week decay).
            is_lotto: Whether this is a lotto play.
            is_rollup: Whether this is a roll up.
            original_trade_profit: Profit from the original trade (for lotto/rollup sizing).

        Returns:
            Number of contracts to buy (0 = skip the trade).
        """
        self.reset_daily_if_needed()

        if self._account_equity <= 0:
            logger.error("Account equity is $0 — cannot size position")
            return 0

        contract_cost = signal_price * 100  # Options are priced per share, 100 shares/contract

        if contract_cost <= 0:
            logger.error("Invalid contract cost: $%.2f", contract_cost)
            return 0

        # Step 1: Base max trade value (1-5% of account)
        max_trade_value = self.get_max_trade_value()
        if max_trade_value <= 0:
            return 0

        # Step 2: Apply day-of-week decay based on days to expiration
        dow_mult = self._get_dow_multiplier(expiry)
        adjusted_value = max_trade_value * dow_mult

        # Step 3: Handle lotto/rollup — only risk portion of profits
        if is_lotto or is_rollup:
            if original_trade_profit > 0:
                lotto_budget = original_trade_profit * LOTTO_PROFIT_FRACTION
                adjusted_value = min(adjusted_value, lotto_budget)
                logger.info(
                    "Lotto/rollup: capping at $%.2f (%.0f%% of $%.2f profit)",
                    lotto_budget, LOTTO_PROFIT_FRACTION * 100, original_trade_profit,
                )
            else:
                # No profit data — use minimum sizing
                min_trade_value = self._account_equity * (config.MIN_TRADE_PCT / 100.0)
                adjusted_value = min(adjusted_value, min_trade_value)
                logger.info("Lotto/rollup with no profit data — using min size $%.2f", adjusted_value)

        # Step 4: Calculate contracts
        contracts = int(adjusted_value / contract_cost)
        contracts = max(contracts, 1)  # Always at least 1 contract

        # Step 5: Enforce hard cap
        contracts = min(contracts, config.MAX_POSITION_SIZE)

        # Step 6: Verify total exposure won't exceed max
        if not self._check_exposure(contracts * contract_cost):
            # Try reducing
            contracts = self._reduce_to_fit_exposure(contract_cost)
            if contracts == 0:
                logger.warning("Cannot fit trade within exposure limits — skipping")
                return 0

        # Track daily risk usage
        trade_value = contracts * contract_cost
        self._daily_risk_used += trade_value

        logger.info(
            "Position size: %d contracts @ $%.2f = $%.2f "
            "(%.1f%% of account, dow_mult=%.2f, daily_used=$%.2f)",
            contracts, signal_price, trade_value,
            (trade_value / self._account_equity) * 100,
            dow_mult, self._daily_risk_used,
        )

        return contracts

    def _get_dow_multiplier(self, expiry: Optional[date]) -> float:
        """Get day-of-week position size multiplier.

        If we know the expiry, use days-to-expiry to determine sizing.
        If not, use today's day of week.
        """
        if expiry:
            days_to_exp = (expiry - date.today()).days
            if days_to_exp <= 0:
                return DOW_MULTIPLIERS[4]  # 0DTE — smallest size
            elif days_to_exp == 1:
                return DOW_MULTIPLIERS[3]  # 1DTE
            elif days_to_exp == 2:
                return DOW_MULTIPLIERS[2]
            elif days_to_exp == 3:
                return DOW_MULTIPLIERS[1]
            else:
                return DOW_MULTIPLIERS[0]  # 4+ DTE — full size
        else:
            return DOW_MULTIPLIERS.get(datetime.now().weekday(), 0.65)

    def _get_current_exposure(self) -> float:
        """Get total current exposure from the position tracker.

        This is called by check_exposure and needs the tracker.
        We store the value and let the bot update it.
        """
        return getattr(self, '_current_exposure', 0.0)

    def update_exposure(self, total_exposure: float) -> None:
        """Update the cached total exposure value.

        Called by the bot after querying open positions.
        """
        self._current_exposure = total_exposure

    def _check_exposure(self, additional_cost: float) -> bool:
        """Check if adding this trade keeps us within max exposure.

        Max exposure = 8% of account (hard cap 10%).
        """
        current = self._get_current_exposure()
        new_total = current + additional_cost
        max_exposure = self._account_equity * (config.MAX_EXPOSURE_PCT / 100.0)
        hard_cap = self._account_equity * (config.HARD_CAP_EXPOSURE_PCT / 100.0)

        if new_total > hard_cap:
            logger.warning(
                "HARD CAP: exposure $%.2f + $%.2f = $%.2f exceeds %.0f%% cap ($%.2f)",
                current, additional_cost, new_total,
                config.HARD_CAP_EXPOSURE_PCT, hard_cap,
            )
            return False

        if new_total > max_exposure:
            logger.warning(
                "Exposure $%.2f + $%.2f = $%.2f exceeds %.0f%% target ($%.2f) — proceeding with caution",
                current, additional_cost, new_total,
                config.MAX_EXPOSURE_PCT, max_exposure,
            )

        return True

    def _reduce_to_fit_exposure(self, contract_cost: float) -> int:
        """Try to find a smaller quantity that fits within exposure limits."""
        current = self._get_current_exposure()
        hard_cap = self._account_equity * (config.HARD_CAP_EXPOSURE_PCT / 100.0)
        remaining = hard_cap - current

        if remaining <= 0:
            return 0

        contracts = int(remaining / contract_cost)
        return max(0, contracts)

    def get_status(self) -> dict:
        """Return current risk status for logging/display."""
        current_exposure = self._get_current_exposure()
        exposure_pct = (current_exposure / self._account_equity * 100) if self._account_equity > 0 else 0

        return {
            "account_equity": self._account_equity,
            "buying_power": self._buying_power,
            "current_exposure": current_exposure,
            "exposure_pct": f"{exposure_pct:.1f}%",
            "max_exposure_pct": f"{config.MAX_EXPOSURE_PCT}%",
            "daily_risk_used": self._daily_risk_used,
            "daily_risk_budget": self._daily_risk_budget,
            "weekly_pnl": self._weekly_pnl,
        }
