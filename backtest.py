"""Backtester - replay historical signals against our risk management system.

Uses the signal_history.json file to simulate trading with configurable
starting capital and risk parameters. Since we only have entry/exit prices
from the signals (not real market data), we assume market orders fill at
the signal price.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# --- Configuration ------------------------------------------------------------

STARTING_CAPITAL = 10_000.0

# Risk parameters (mirror config.py)
MIN_TRADE_PCT = 1.0
MAX_TRADE_PCT = 5.0
MAX_EXPOSURE_PCT = 8.0
HARD_CAP_EXPOSURE_PCT = 10.0
MAX_POSITION_SIZE = 10

# Day-of-week multipliers (days to expiry)
DTE_MULTIPLIERS = {0: 0.30, 1: 0.40, 2: 0.65, 3: 0.75, 4: 1.0}  # 0DTE->4+DTE

LOTTO_PROFIT_FRACTION = 0.35

# Only trade these tickers
ALLOWED_TICKERS = {"SPY", "QQQ"}


# --- Data Structures ---------------------------------------------------------

@dataclass
class Position:
    ticker: str
    strike: float
    option_type: str  # "C" or "P"
    entry_price: float
    quantity: int
    entry_date: datetime
    note: str = ""

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity * 100


@dataclass
class ClosedTrade:
    ticker: str
    strike: float
    option_type: str
    entry_price: float
    exit_price: float
    quantity: int
    entry_date: datetime
    exit_date: datetime
    note: str = ""

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity * 100

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100


@dataclass
class WeeklyStats:
    week_start: date
    starting_equity: float
    ending_equity: float
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0

    @property
    def return_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity * 100


# --- Signal Parser (standalone for backtest) ----------------------------------

_BUY_WITH_EXPIRY = re.compile(
    r"BOUGHT\s+(?P<ticker>[A-Z]{1,5})\s+"
    r"(?P<expiry>\d{1,2}/\d{1,2})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt>[CP])\s+"
    r"(?P<price>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<note>.+))?",
    re.IGNORECASE,
)

_BUY_NO_EXPIRY = re.compile(
    r"BOUGHT\s+(?P<ticker>[A-Z]{1,5})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt>[CP])\s+"
    r"(?P<price>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<note>.+))?",
    re.IGNORECASE,
)

_SELL_PATTERN = re.compile(
    r"SOLD\s+(?P<ticker>[A-Z]{1,5})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt>[CP])\s+"
    r"(?P<price>\d+(?:\.\d+)?)\s+"
    r"(?P<size>.+)",
    re.IGNORECASE,
)


def parse_sell_fraction(size_str: str) -> float:
    normalized = size_str.strip().upper()
    if "ALL" in normalized:
        return 1.0
    frac_match = re.search(r"(\d+)\s*/\s*(\d+)", normalized)
    if frac_match:
        num, den = int(frac_match.group(1)), int(frac_match.group(2))
        return num / den if den > 0 else 1.0
    return 1.0


# --- Backtester ---------------------------------------------------------------

class Backtester:
    def __init__(self, starting_capital: float = STARTING_CAPITAL) -> None:
        self.starting_capital = starting_capital
        self.equity = starting_capital
        self.cash = starting_capital
        self.positions: list[Position] = []
        self.closed_trades: list[ClosedTrade] = []
        self.weekly_stats: list[WeeklyStats] = []
        self.skipped_signals = 0
        self.failed_matches = 0
        self._last_week_pnl = 0.0
        self._current_week_start: Optional[date] = None
        self._week_start_equity = starting_capital
        self._week_trades = 0
        self._week_wins = 0
        self._week_losses = 0
        self._week_pnl = 0.0

    @property
    def total_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions)

    @property
    def exposure_pct(self) -> float:
        return (self.total_exposure / self.equity * 100) if self.equity > 0 else 0

    def _get_dte_multiplier(self, msg_date: datetime, expiry_str: Optional[str]) -> float:
        if not expiry_str:
            # No expiry info - use day of week as proxy
            dow = msg_date.weekday()
            return DTE_MULTIPLIERS.get(min(dow, 4), 0.65)

        parts = expiry_str.split("/")
        month, day = int(parts[0]), int(parts[1])
        year = msg_date.year
        try:
            exp_date = date(year, month, day)
            if exp_date < msg_date.date():
                exp_date = date(year + 1, month, day)
        except ValueError:
            return 0.65

        dte = (exp_date - msg_date.date()).days
        if dte <= 0:
            return DTE_MULTIPLIERS[0]
        elif dte == 1:
            return DTE_MULTIPLIERS[1]
        elif dte == 2:
            return DTE_MULTIPLIERS[2]
        elif dte == 3:
            return DTE_MULTIPLIERS[3]
        else:
            return DTE_MULTIPLIERS[4]

    def _calculate_buy_quantity(
        self,
        price: float,
        msg_date: datetime,
        expiry_str: Optional[str],
        note: str,
    ) -> int:
        """Risk-managed position sizing."""
        contract_cost = price * 100
        if contract_cost <= 0 or self.equity <= 0:
            return 0

        # Base: up to MAX_TRADE_PCT of equity
        max_trade_value = self.equity * (MAX_TRADE_PCT / 100.0)

        # DTE decay
        dte_mult = self._get_dte_multiplier(msg_date, expiry_str)
        adjusted = max_trade_value * dte_mult

        # Lotto/rollup: cap at fraction of recent profits
        note_lower = note.lower() if note else ""
        if "lotto" in note_lower or "roll" in note_lower:
            if self._last_week_pnl > 0:
                adjusted = min(adjusted, self._last_week_pnl * LOTTO_PROFIT_FRACTION)
            else:
                adjusted = min(adjusted, self.equity * (MIN_TRADE_PCT / 100.0))

        contracts = max(1, int(adjusted / contract_cost))
        contracts = min(contracts, MAX_POSITION_SIZE)

        # Check total exposure
        new_exposure = self.total_exposure + (contracts * contract_cost)
        hard_cap = self.equity * (HARD_CAP_EXPOSURE_PCT / 100.0)
        if new_exposure > hard_cap:
            remaining = hard_cap - self.total_exposure
            if remaining <= 0:
                return 0
            contracts = max(1, int(remaining / contract_cost))

        # Check we have enough cash
        total_cost = contracts * contract_cost
        if total_cost > self.cash:
            contracts = max(1, int(self.cash / contract_cost))
            if contracts * contract_cost > self.cash:
                return 0

        return contracts

    def _track_week(self, msg_date: datetime) -> None:
        """Track weekly stats, rolling over on Monday."""
        current_week = msg_date.date() - timedelta(days=msg_date.weekday())

        if self._current_week_start is None:
            self._current_week_start = current_week
            self._week_start_equity = self.equity
            return

        if current_week != self._current_week_start:
            # Save completed week
            ws = WeeklyStats(
                week_start=self._current_week_start,
                starting_equity=self._week_start_equity,
                ending_equity=self.equity,
                trades_taken=self._week_trades,
                wins=self._week_wins,
                losses=self._week_losses,
                total_pnl=self._week_pnl,
            )
            self.weekly_stats.append(ws)

            self._last_week_pnl = self._week_pnl
            self._current_week_start = current_week
            self._week_start_equity = self.equity
            self._week_trades = 0
            self._week_wins = 0
            self._week_losses = 0
            self._week_pnl = 0.0

    def process_message(self, content: str, timestamp: str) -> None:
        """Process a single historical message."""
        msg_date = datetime.fromisoformat(timestamp)
        self._track_week(msg_date)

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.upper().startswith("BOUGHT"):
                self._process_buy(line, msg_date)
            elif line.upper().startswith("SOLD"):
                self._process_sell(line, msg_date)

    def _process_buy(self, line: str, msg_date: datetime) -> None:
        # Try with expiry first, then without
        m = _BUY_WITH_EXPIRY.match(line.strip())
        expiry_str = None
        if m:
            expiry_str = m.group("expiry")
        else:
            m = _BUY_NO_EXPIRY.match(line.strip())

        if not m:
            self.skipped_signals += 1
            return

        ticker = m.group("ticker").upper()
        if ticker not in ALLOWED_TICKERS:
            return

        strike = float(m.group("strike"))
        opt = m.group("opt").upper()
        price = float(m.group("price"))
        note = (m.group("note") or "").strip()

        # For "avg X.XX" notes, use the avg price instead (that's the real cost basis)
        avg_match = re.search(r"avg\s+(\d+(?:\.\d+)?)", note, re.IGNORECASE)
        if avg_match:
            price = float(avg_match.group(1))

        quantity = self._calculate_buy_quantity(price, msg_date, expiry_str, note)
        if quantity == 0:
            self.skipped_signals += 1
            return

        cost = price * quantity * 100
        self.cash -= cost

        self.positions.append(Position(
            ticker=ticker, strike=strike, option_type=opt,
            entry_price=price, quantity=quantity,
            entry_date=msg_date, note=note,
        ))

    def _process_sell(self, line: str, msg_date: datetime) -> None:
        m = _SELL_PATTERN.match(line.strip())
        if not m:
            self.skipped_signals += 1
            return

        ticker = m.group("ticker").upper()
        if ticker not in ALLOWED_TICKERS:
            return

        strike = float(m.group("strike"))
        opt = m.group("opt").upper()
        price = float(m.group("price"))
        size_str = m.group("size").strip()

        # Find matching position
        pos = self._find_position(ticker, strike, opt)
        if pos is None:
            self.failed_matches += 1
            return

        fraction = parse_sell_fraction(size_str)
        sell_qty = max(1, int(pos.quantity * fraction)) if fraction < 1.0 else pos.quantity
        sell_qty = min(sell_qty, pos.quantity)

        # Record closed trade
        ct = ClosedTrade(
            ticker=ticker, strike=strike, option_type=opt,
            entry_price=pos.entry_price, exit_price=price,
            quantity=sell_qty, entry_date=pos.entry_date,
            exit_date=msg_date, note=pos.note,
        )
        self.closed_trades.append(ct)

        # Update cash and equity
        revenue = price * sell_qty * 100
        self.cash += revenue
        self.equity += ct.pnl

        # Track weekly stats
        self._week_trades += 1
        self._week_pnl += ct.pnl
        if ct.pnl >= 0:
            self._week_wins += 1
        else:
            self._week_losses += 1

        # Update or remove position
        remaining = pos.quantity - sell_qty
        if remaining <= 0:
            self.positions.remove(pos)
        else:
            pos.quantity = remaining

    def _find_position(self, ticker: str, strike: float, opt: str) -> Optional[Position]:
        for pos in self.positions:
            if pos.ticker == ticker and pos.strike == strike and pos.option_type == opt:
                return pos
        return None

    def finalize(self) -> None:
        """Close out any remaining open positions at $0 (expired worthless) and finalize stats."""
        # Save final week
        if self._current_week_start:
            ws = WeeklyStats(
                week_start=self._current_week_start,
                starting_equity=self._week_start_equity,
                ending_equity=self.equity,
                trades_taken=self._week_trades,
                wins=self._week_wins,
                losses=self._week_losses,
                total_pnl=self._week_pnl,
            )
            self.weekly_stats.append(ws)

        # Close remaining positions as total loss
        for pos in list(self.positions):
            ct = ClosedTrade(
                ticker=pos.ticker, strike=pos.strike, option_type=pos.option_type,
                entry_price=pos.entry_price, exit_price=0.0,
                quantity=pos.quantity, entry_date=pos.entry_date,
                exit_date=datetime.now(),
                note=pos.note + " [expired/unsold]",
            )
            self.closed_trades.append(ct)
            self.equity += ct.pnl

        self.positions.clear()

    def print_report(self) -> None:
        """Print comprehensive backtest results."""
        self.finalize()

        total_trades = len(self.closed_trades)
        wins = [t for t in self.closed_trades if t.pnl > 0]
        losses = [t for t in self.closed_trades if t.pnl < 0]
        breakeven = [t for t in self.closed_trades if t.pnl == 0]

        total_pnl = sum(t.pnl for t in self.closed_trades)
        total_wins = sum(t.pnl for t in wins)
        total_losses = sum(t.pnl for t in losses)
        avg_win = total_wins / len(wins) if wins else 0
        avg_loss = total_losses / len(losses) if losses else 0

        print("=" * 70)
        print("                    BACKTEST RESULTS")
        print("=" * 70)
        print(f"  Period:            {self.weekly_stats[0].week_start if self.weekly_stats else 'N/A'}"
              f" -> {self.weekly_stats[-1].week_start if self.weekly_stats else 'N/A'}")
        print(f"  Starting Capital:  ${self.starting_capital:,.2f}")
        print(f"  Final Equity:      ${self.equity:,.2f}")
        print(f"  Total Return:      ${total_pnl:,.2f}"
              f" ({total_pnl/self.starting_capital*100:+.1f}%)")
        print()
        print("--- Trade Statistics -----------------------------------------------")
        print(f"  Total Trades:      {total_trades}")
        print(f"  Winners:           {len(wins)} ({len(wins)/total_trades*100:.1f}%)" if total_trades else "")
        print(f"  Losers:            {len(losses)} ({len(losses)/total_trades*100:.1f}%)" if total_trades else "")
        print(f"  Breakeven:         {len(breakeven)}")
        print(f"  Skipped Signals:   {self.skipped_signals}")
        print(f"  Unmatched Sells:   {self.failed_matches}")
        print()
        print("--- P&L Breakdown -------------------------------------------------")
        print(f"  Total Won:         ${total_wins:,.2f}")
        print(f"  Total Lost:        ${total_losses:,.2f}")
        print(f"  Avg Win:           ${avg_win:,.2f}")
        print(f"  Avg Loss:          ${avg_loss:,.2f}")
        if avg_loss != 0:
            print(f"  Profit Factor:     {abs(total_wins/total_losses):.2f}")
            print(f"  Avg Win/Avg Loss:  {abs(avg_win/avg_loss):.2f}")
        print()

        # Monthly returns
        print("--- Monthly Returns -----------------------------------------------")
        monthly: dict[str, float] = {}
        for t in self.closed_trades:
            key = t.exit_date.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0) + t.pnl

        # Show last 12 months
        sorted_months = sorted(monthly.keys())
        for m in sorted_months[-12:]:
            bar_len = int(abs(monthly[m]) / max(abs(v) for v in monthly.values()) * 30) if monthly.values() else 0
            bar = "#" * bar_len
            sign = "+" if monthly[m] >= 0 else ""
            print(f"  {m}:  {sign}${monthly[m]:>9,.2f}  {bar}")

        print()
        print("--- Weekly Stats Summary ------------------------------------------")
        if self.weekly_stats:
            green_weeks = sum(1 for w in self.weekly_stats if w.total_pnl > 0)
            red_weeks = sum(1 for w in self.weekly_stats if w.total_pnl < 0)
            flat_weeks = sum(1 for w in self.weekly_stats if w.total_pnl == 0)
            best_week = max(self.weekly_stats, key=lambda w: w.total_pnl)
            worst_week = min(self.weekly_stats, key=lambda w: w.total_pnl)
            print(f"  Total Weeks:       {len(self.weekly_stats)}")
            print(f"  Green Weeks:       {green_weeks} ({green_weeks/len(self.weekly_stats)*100:.1f}%)")
            print(f"  Red Weeks:         {red_weeks} ({red_weeks/len(self.weekly_stats)*100:.1f}%)")
            print(f"  Flat Weeks:        {flat_weeks}")
            print(f"  Best Week:         ${best_week.total_pnl:+,.2f} (w/o {best_week.week_start})")
            print(f"  Worst Week:        ${worst_week.total_pnl:+,.2f} (w/o {worst_week.week_start})")

            # Max drawdown
            peak = self.starting_capital
            max_dd = 0.0
            max_dd_pct = 0.0
            running_equity = self.starting_capital
            for w in self.weekly_stats:
                running_equity = w.ending_equity
                if running_equity > peak:
                    peak = running_equity
                dd = peak - running_equity
                dd_pct = dd / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
                    max_dd_pct = dd_pct

            print(f"\n  Max Drawdown:      ${max_dd:,.2f} ({max_dd_pct:.1f}%)")

        # Equity curve (quarterly)
        print()
        print("--- Equity Curve (Quarterly) ---------------------------------------")
        if self.weekly_stats:
            quarterly: dict[str, float] = {}
            for w in self.weekly_stats:
                q = f"{w.week_start.year}-Q{(w.week_start.month-1)//3+1}"
                quarterly[q] = w.ending_equity

            for q, eq in sorted(quarterly.items()):
                pct = (eq - self.starting_capital) / self.starting_capital * 100
                bar_len = max(0, int(pct / 5))
                bar = "#" * min(bar_len, 40)
                print(f"  {q}:  ${eq:>10,.2f}  ({pct:+6.1f}%)  {bar}")

        print()
        print("=" * 70)


# --- Main ---------------------------------------------------------------------

def main():
    history_path = Path(__file__).parent / "signal_history.json"
    if not history_path.exists():
        print("ERROR: signal_history.json not found. Run fetch_history.py first.")
        return

    with open(history_path) as f:
        messages = json.load(f)

    print(f"Loaded {len(messages)} messages")

    # Run with risk management
    print(f"\n{'='*70}")
    print(f"  SCENARIO 1: With Risk Management (${STARTING_CAPITAL:,.0f} capital)")
    print(f"{'='*70}\n")

    bt1 = Backtester(STARTING_CAPITAL)
    for msg in messages:
        if msg["author"] == "grailedmund":
            bt1.process_message(msg["content"], msg["timestamp"])
    bt1.print_report()

    # Run without exposure limits for comparison
    print(f"\n{'='*70}")
    print(f"  SCENARIO 2: No Exposure Limits (${STARTING_CAPITAL:,.0f} capital, 1 contract each)")
    print(f"{'='*70}\n")

    bt2 = BacktesterSimple(STARTING_CAPITAL)
    for msg in messages:
        if msg["author"] == "grailedmund":
            bt2.process_message(msg["content"], msg["timestamp"])
    bt2.print_report()


class BacktesterSimple(Backtester):
    """Simplified backtester: always buy 1 contract, no exposure limits."""

    def _calculate_buy_quantity(self, price, msg_date, expiry_str, note):
        cost = price * 100
        if cost > self.cash:
            return 0
        return 1


if __name__ == "__main__":
    main()
