"""Benchmark: times every step of the signal-to-order pipeline.

Hits real Alpaca paper API to measure actual network latency.
Does NOT place real orders — uses a dry_run flag to stop before submit_order.

Usage:
    python benchmark.py              # dry run (no orders placed)
    python benchmark.py --live       # actually submit a paper order
"""

from __future__ import annotations

import asyncio
import sys
import time

import config
from broker import AlpacaBroker
from parser import parse_message
from positions import PositionTracker
from risk_manager import RiskManager


# Simulated Discord signal (typical grailedmund format)
SAMPLE_SIGNAL = "BOUGHT SPY 4/11 520C 1.50"


def timed(label: str, func, *args, **kwargs):
    """Run a sync function and print elapsed time."""
    start = time.perf_counter()
    result = func(*args, **kwargs)
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  {label:<40} {elapsed:>8.1f} ms")
    return result, elapsed


async def timed_async(label: str, coro):
    """Await a coroutine and print elapsed time."""
    start = time.perf_counter()
    result = await coro
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  {label:<40} {elapsed:>8.1f} ms")
    return result, elapsed


async def run_benchmark(live: bool = False):
    total_start = time.perf_counter()

    print("=" * 60)
    print(f"  SIGNAL-TO-ORDER BENCHMARK {'(LIVE)' if live else '(DRY RUN)'}")
    print("=" * 60)
    print(f"  Signal: {SAMPLE_SIGNAL}")
    print("-" * 60)

    # Step 1: Parse (should be near-instant)
    signals, t_parse = timed("1. parse_message()", parse_message, SAMPLE_SIGNAL)
    signal = signals[0] if signals else None
    if not signal:
        print("  FAILED: could not parse signal")
        return

    # Step 2: Connect broker
    broker = AlpacaBroker()
    _, t_connect = timed("2. broker.connect()", broker.connect)

    # Step 3: Get account (risk state refresh)
    account, t_account = timed("3. broker.get_account()", broker.get_account)

    # Step 4: Risk sizing (local math)
    risk = RiskManager()
    equity = float(account["equity"])
    buying_power = float(account["buying_power"])
    risk.update_account(equity, buying_power)

    tracker = PositionTracker(":memory:")
    risk.update_exposure(0.0)

    quantity, t_risk = timed(
        "4. risk.calculate_position_size()",
        risk.calculate_position_size,
        signal_price=signal.price,
    )

    # Step 5: Submit order (the critical call)
    if live and quantity > 0:
        try:
            order, t_order = timed("5. broker.buy_option() [LIVE]", broker.buy_option, signal, quantity)
            print(f"     Order result: {order}")
        except Exception as exc:
            t_order = (time.perf_counter() - (total_start + (t_parse + t_connect + t_account + t_risk) / 1000)) * 1000
            # Re-measure just the failed call to get accurate API round-trip
            start = time.perf_counter()
            try:
                broker.buy_option(signal, quantity)
            except Exception:
                pass
            t_order = (time.perf_counter() - start) * 1000
            print(f"  {'5. broker.buy_option() [FAILED]':<40} {t_order:>8.1f} ms")
            print(f"     Error: {exc}")
            print(f"     (API round-trip still measured — contract may not exist on paper)")
    else:
        # Dry run: just build the OCC symbol and check buying power
        _, t_order = timed("5. broker.check_buying_power() [DRY]", broker.check_buying_power, signal.price * quantity * 100)
        print(f"     (skipped submit_order — dry run, use --live to place paper order)")

    # Step 6: Concurrent parse + risk refresh (what actually happens now)
    print()
    print("  Simulating actual async pipeline:")
    start = time.perf_counter()
    await asyncio.gather(
        asyncio.to_thread(parse_message, SAMPLE_SIGNAL),
        asyncio.to_thread(broker.get_account),
    )
    t_parallel = (time.perf_counter() - start) * 1000
    print(f"  {'6. gather(parse, risk_refresh)':<40} {t_parallel:>8.1f} ms")
    print()
    print("  Notification: Discord DM (non-blocking, not benchmarked)")

    # Summary
    critical_path = t_parse + t_account + t_risk + t_order
    actual_path = t_parallel + t_risk + t_order
    total = (time.perf_counter() - total_start) * 1000

    print()
    print("=" * 60)
    print(f"  CRITICAL PATH (sequential):              {critical_path:>8.1f} ms")
    print(f"  CRITICAL PATH (async pipeline):          {actual_path:>8.1f} ms")
    print(f"  Broker connect (one-time at startup):    {t_connect:>8.1f} ms")
    print(f"  Total benchmark time:                    {total:>8.1f} ms")
    print("=" * 60)

    if t_account > 2000:
        print("\n  ⚠ Alpaca API is slow (>2s). This is network latency, not code.")


if __name__ == "__main__":
    live = "--live" in sys.argv
    asyncio.run(run_benchmark(live=live))
