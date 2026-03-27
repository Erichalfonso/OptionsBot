"""Email notifications for trading signals."""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import config
from logger_setup import setup_logger

logger = setup_logger("optionsbot.notifier")


def send_signal_email(signal_text: str) -> None:
    """Send a trading signal notification via email.

    Args:
        signal_text: The raw signal message from Discord.
    """
    if not config.EMAIL_ADDRESS or not config.EMAIL_APP_PASSWORD:
        logger.debug("Email not configured — skipping notification")
        return

    msg = MIMEText(signal_text)
    msg["Subject"] = "OptionsBot Signal"
    msg["From"] = config.EMAIL_ADDRESS
    msg["To"] = config.EMAIL_ADDRESS

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Signal email sent")
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)
