"""Main entry point — Discord bot that listens for options trading signals.

Optimized for speed: minimal processing between signal receipt and order execution.
Risk management enforced on every trade per Optionsful guidelines.
"""

from __future__ import annotations

import asyncio
from datetime import date

import discord

import config
from broker import AlpacaBroker
from logger_setup import setup_logger
from notifier import send_signal_email
from parser import Signal, parse_message, parse_sell_size
from positions import PositionTracker
from risk_manager import RiskManager

logger = setup_logger("optionsbot")

# Discord intents
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Trading components
broker = AlpacaBroker()
tracker = PositionTracker(config.DB_PATH)
risk = RiskManager()


def _update_risk_state() -> None:
    """Refresh risk manager with current account and position data."""
    try:
        account = broker.get_account()
        equity = float(account["equity"])
        buying_power = float(account["buying_power"])
        risk.update_account(equity, buying_power)

        # Calculate current exposure from open positions
        open_positions = tracker.get_open_positions()
        total_exposure = sum(
            pos["price"] * pos["quantity"] * 100 for pos in open_positions
        )
        risk.update_exposure(total_exposure)

    except Exception as exc:
        logger.error("Failed to update risk state: %s", exc)


@client.event
async def on_ready() -> None:
    """Called when the bot has connected to Discord."""
    logger.info("Bot connected as %s", client.user)
    logger.info("Listening on channel %d for author '%s'", config.CHANNEL_ID, config.SIGNAL_AUTHOR)
    logger.info(
        "Risk params: trade=%.0f-%.0f%% of account, exposure cap=%.0f%% (hard %.0f%%)",
        config.MIN_TRADE_PCT, config.MAX_TRADE_PCT,
        config.MAX_EXPOSURE_PCT, config.HARD_CAP_EXPOSURE_PCT,
    )

    try:
        broker.connect()
        _update_risk_state()
        status = risk.get_status()
        logger.info("Risk status: %s", status)
    except Exception as exc:
        logger.error("Failed to connect to Alpaca: %s", exc)
        logger.warning("Bot will continue but trades will fail until Alpaca is configured")


@client.event
async def on_message(message: discord.Message) -> None:
    """Process incoming Discord messages for trading signals."""
    # Fast path: reject early
    if message.author == client.user:
        return
    if message.channel.id != config.CHANNEL_ID:
        return
    if message.author.name != config.SIGNAL_AUTHOR:
        return

    logger.info("Signal received:\n%s", message.content)

    # Fire-and-forget: email runs in background, never blocks trading
    asyncio.create_task(_send_email_background(message.content))

    # Parse + refresh risk state concurrently
    signals, _ = await asyncio.gather(
        asyncio.to_thread(parse_message, message.content),
        asyncio.to_thread(_update_risk_state),
    )
    if not signals:
        logger.info("No valid signals parsed from message")
        return

    logger.info("Parsed %d signal(s) — executing", len(signals))

    for signal in signals:
        try:
            if signal.action == "BUY":
                await handle_buy(signal)
            elif signal.action == "SELL":
                await handle_sell(signal)
        except Exception as exc:
            logger.error("Error processing %s %s: %s", signal.action, signal.ticker, exc, exc_info=True)


async def _send_email_background(content: str) -> None:
    """Send email notification in background — never blocks trading."""
    try:
        await asyncio.to_thread(send_signal_email, content)
    except Exception as exc:
        logger.error("Background email failed: %s", exc)


async def handle_buy(signal: Signal) -> None:
    """Process a BUY signal: calculate position size via risk manager, place market order."""
    note = (signal.note or "").lower()
    is_lotto = "lotto" in note
    is_rollup = "roll" in note

    # For lotto/rollup, try to find the profit from the original trade
    original_profit = 0.0
    if is_lotto or is_rollup:
        original_profit = _get_last_closed_profit(signal.ticker)

    # Let the risk manager determine quantity
    quantity = risk.calculate_position_size(
        signal_price=signal.price,
        is_lotto=is_lotto,
        is_rollup=is_rollup,
        original_trade_profit=original_profit,
    )

    if quantity == 0:
        logger.info("Risk manager says SKIP — %s %s", signal.ticker, signal.note or "")
        return

    logger.info(
        "BUY %s %s %.0f%s @ $%.2f qty=%d (risk-sized)%s",
        signal.ticker, signal.expiry, signal.strike,
        "C" if signal.option_type == "CALL" else "P",
        signal.price, quantity,
        f" [{signal.note}]" if signal.note else "",
    )

    try:
        order_result = await asyncio.to_thread(broker.buy_option, signal, quantity)
        logger.info("Order placed: %s", order_result)
    except Exception as exc:
        logger.error("Broker buy failed: %s", exc)
        tracker.record_trade(signal, quantity, status="FAILED")
        return

    tracker.record_trade(signal, quantity, status="OPEN")

    # Update exposure after trade — fire-and-forget, don't block next signal
    asyncio.create_task(asyncio.to_thread(_update_risk_state))


def _get_last_closed_profit(ticker: str) -> float:
    """Get realized profit from the last fully closed position for this ticker.

    Used for lotto/rollup sizing per grailedmund's rule:
    'I made $1000 on a trade, I would take $300-400 and put it into a roll up.'
    """
    try:
        return tracker.get_last_closed_profit(ticker)
    except Exception:
        return 0.0


async def handle_sell(signal: Signal) -> None:
    """Process a SELL signal: find position, place market order, update records."""
    position = tracker.get_position_for_signal(signal)
    if position is None:
        logger.warning(
            "No open position for SELL %s %.0f%s — skipping",
            signal.ticker, signal.strike,
            "C" if signal.option_type == "CALL" else "P",
        )
        return

    current_qty = position["quantity"]
    sell_fraction = parse_sell_size(signal.size) if signal.size else 1.0
    if sell_fraction is None:
        sell_fraction = 1.0

    sell_qty = broker._calculate_sell_quantity(current_qty, sell_fraction)

    # SELL signals don't include expiry — get it from the open position
    if signal.expiry is None and position.get("expiry"):
        signal.expiry = date.fromisoformat(position["expiry"])

    logger.info(
        "SELL %s %.0f%s @ $%.2f — %s (%d of %d contracts)",
        signal.ticker, signal.strike,
        "C" if signal.option_type == "CALL" else "P",
        signal.price, signal.size, sell_qty, current_qty,
    )

    try:
        order_result = await asyncio.to_thread(broker.sell_option, signal, current_quantity=current_qty)
        logger.info("Sell order placed: %s", order_result)
    except Exception as exc:
        logger.error("Broker sell failed: %s", exc)
        tracker.record_trade(signal, sell_qty, status="FAILED")
        return

    tracker.record_trade(signal, sell_qty, status="CLOSED")

    remaining = current_qty - sell_qty
    if remaining <= 0:
        tracker.close_position(position["id"])
        pnl = tracker.calculate_pnl(signal.ticker, signal.strike, signal.option_type)
        if pnl is not None:
            logger.info("Position closed — realized P&L: $%.2f", pnl)
    else:
        tracker.update_position_quantity(position["id"], remaining)
        tracker.update_position_status(position["id"], "PARTIAL")
        logger.info("Partial sell — %d contracts remaining", remaining)

    # Update risk state after sell — fire-and-forget
    asyncio.create_task(asyncio.to_thread(_update_risk_state))


def main() -> None:
    """Start the Discord bot."""
    if not config.DISCORD_TOKEN:
        logger.error("DISCORD_BOT_TOKEN not set in .env — cannot start bot")
        return

    logger.info("Starting OptionsBot...")
    client.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
