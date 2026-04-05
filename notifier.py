"""Notifications for trading signals via Discord DM."""

from __future__ import annotations

import discord

import config
from logger_setup import setup_logger

logger = setup_logger("optionsbot.notifier")


async def send_discord_dm(client: discord.Client, signal_text: str) -> None:
    """Send a trading signal as a Discord DM to the configured user."""
    if not config.NOTIFY_USER_ID:
        logger.debug("NOTIFY_USER_ID not configured — skipping DM")
        return

    try:
        user = await client.fetch_user(config.NOTIFY_USER_ID)
        await user.send(f"**OptionsBot Signal**\n```\n{signal_text}\n```")
        logger.info("Signal DM sent to user %d", config.NOTIFY_USER_ID)
    except Exception as exc:
        logger.error("Failed to send DM: %s", exc)
