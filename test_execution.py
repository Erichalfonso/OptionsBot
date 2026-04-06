"""Test end-to-end signal → order execution latency against Alpaca paper account.

Simulates exactly what on_message does: parse → risk check → broker order → fill.
Uses a cheap option to minimize paper account impact.
"""

import asyncio
import time
from datetime import date, datetime, timezone

from broker import AlpacaBroker
from logger_setup import setup_logger
from parser import parse_message
from positions import PositionTracker
from risk_manager import RiskManager

logger = setup_logger("test_execution")


async def simulate_signal(message_text: str) -> None:
    """Simulate a Discord signal message and measure execution latency."""
    t_start = time.perf_counter()
    signal_time = datetime.now(timezone.utc)

    print(f"\n{'='*60}")
    print(f"Signal received at: {signal_time.isoformat()}")
    print(f"Message: {message_text}")
    print(f"{'='*60}\n")

    # --- Step 1: Parse (same as bot.py) ---
    signals = parse_message(message_text)
    t_parsed = time.perf_counter()
    print(f"[{(t_parsed - t_start)*1000:6.0f}ms] Parsed {len(signals)} signal(s)")

    if not signals:
        print("No valid signals found.")
        return

    for sig in signals:
        print(f"  > {sig.action} {sig.ticker} {sig.expiry} {sig.strike}{sig.option_type[0]} @ ${sig.price}")

    # --- Step 2: Connect broker + risk check ---
    broker = AlpacaBroker()
    broker.connect()
    t_connected = time.perf_counter()
    print(f"[{(t_connected - t_start)*1000:6.0f}ms] Broker connected")

    tracker = PositionTracker(":memory:")
    risk = RiskManager()

    account = broker.get_account()
    equity = float(account["equity"])
    buying_power = float(account["buying_power"])
    risk.update_account(equity, buying_power)
    risk.update_exposure(0)  # fresh test, no existing exposure
    t_risk = time.perf_counter()
    print(f"[{(t_risk - t_start)*1000:6.0f}ms] Risk state updated (equity=${equity:,.2f})")

    # --- Step 3: Execute each signal ---
    for sig in signals:
        if sig.action == "BUY":
            qty = risk.calculate_position_size(signal_price=sig.price)
            if qty == 0:
                print(f"[  SKIP ] Risk manager rejected {sig.ticker}")
                continue

            print(f"\n  Placing BUY: {qty}x {sig.ticker} {sig.expiry} {sig.strike}{sig.option_type[0]} @ ${sig.price}")

            t_order = time.perf_counter()
            try:
                result = await asyncio.to_thread(broker.buy_option, sig, qty)
                t_filled = time.perf_counter()
                print(f"[{(t_filled - t_start)*1000:6.0f}ms] Order submitted in {(t_filled - t_order)*1000:.0f}ms")
                print(f"  Result: {result}")
            except Exception as exc:
                t_err = time.perf_counter()
                print(f"[{(t_err - t_start)*1000:6.0f}ms] Order FAILED in {(t_err - t_order)*1000:.0f}ms: {exc}")

        elif sig.action == "SELL":
            print(f"\n  Placing SELL: {sig.ticker} {sig.strike}{sig.option_type[0]} @ ${sig.price}")

            t_order = time.perf_counter()
            try:
                result = await asyncio.to_thread(broker.sell_option, sig, current_quantity=1)
                t_filled = time.perf_counter()
                print(f"[{(t_filled - t_start)*1000:6.0f}ms] Sell submitted in {(t_filled - t_order)*1000:.0f}ms")
                print(f"  Result: {result}")
            except Exception as exc:
                t_err = time.perf_counter()
                print(f"[{(t_err - t_start)*1000:6.0f}ms] Sell FAILED in {(t_err - t_order)*1000:.0f}ms: {exc}")

    t_end = time.perf_counter()
    print(f"\n{'='*60}")
    print(f"Total signal-to-done: {(t_end - t_start)*1000:.0f}ms")
    print(f"{'='*60}")


if __name__ == "__main__":
    # Simulate a real signal — use a cheap option with valid expiry
    # Change these to match a real contract on Alpaca paper
    test_message = "BOUGHT SPY 4/8 550C 0.50"

    print("OptionsBot Execution Latency Test")
    print("Placing order on Alpaca PAPER account...\n")
    asyncio.run(simulate_signal(test_message))
