"""Signal message parser for Discord options trading alerts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from logger_setup import setup_logger

logger = setup_logger("optionsbot.parser")


@dataclass
class Signal:
    """Represents a parsed trading signal."""

    action: str  # "BUY" or "SELL"
    ticker: str  # e.g. "SPY", "QQQ"
    expiry: Optional[date]  # expiration date (None for SELL signals without expiry)
    strike: float  # strike price
    option_type: str  # "CALL" or "PUT"
    price: float  # entry/exit price
    size: Optional[str] = None  # sell size: "1/4 position", "ALL OUT", etc.
    note: Optional[str] = None  # buy note: "lotto", "roll up", etc.
    raw: str = ""  # original signal text

    @property
    def occ_symbol(self) -> str:
        """Format as OCC option symbol: SPY250320P00657000.

        Requires expiry to be set.
        """
        if self.expiry is None:
            raise ValueError("Cannot build OCC symbol without expiry date")

        date_str = self.expiry.strftime("%y%m%d")
        opt_char = "C" if self.option_type == "CALL" else "P"
        # Strike in OCC format: price * 1000, zero-padded to 8 digits
        strike_int = int(self.strike * 1000)
        strike_str = f"{strike_int:08d}"
        return f"{self.ticker}{date_str}{opt_char}{strike_str}"


# Pattern for BUY signals WITH expiry:
# BOUGHT {TICKER} {EXPIRY} {STRIKE}{C/P} [@] {PRICE} [notes...]
_BUY_PATTERN = re.compile(
    r"BOUGHT\s+"
    r"(?P<ticker>[A-Z]{1,5})\s+"
    r"(?P<expiry>\d{1,2}/\d{1,2})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt_type>[CP])\s+"
    r"@?\s*(?P<price>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<note>.+))?",
    re.IGNORECASE,
)

# Pattern for BUY signals WITHOUT expiry (e.g. "BOUGHT SPY 657P 2.29"):
_BUY_NO_EXPIRY_PATTERN = re.compile(
    r"BOUGHT\s+"
    r"(?P<ticker>[A-Z]{1,5})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt_type>[CP])\s+"
    r"@?\s*(?P<price>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<note>.+))?",
    re.IGNORECASE,
)

# Pattern for SELL signals:
# SOLD {TICKER} {STRIKE}{C/P} [@] {PRICE} {SIZE}
_SELL_PATTERN = re.compile(
    r"SOLD\s+"
    r"(?P<ticker>[A-Z]{1,5})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt_type>[CP])\s+"
    r"@?\s*(?P<price>\d+(?:\.\d+)?)\s+"
    r"(?P<size>.+)",
    re.IGNORECASE,
)

# Loose patterns to detect signals the strict patterns miss
_LOOSE_BUY = re.compile(r"BOUGHT", re.IGNORECASE)
_LOOSE_SELL = re.compile(r"SOLD", re.IGNORECASE)


def _parse_expiry(expiry_str: str) -> date:
    """Parse M/DD expiry string and append current year.

    If the resulting date is in the past, assume next year.

    Args:
        expiry_str: Date string like "3/20" or "12/5".

    Returns:
        Parsed date object.
    """
    parts = expiry_str.split("/")
    month = int(parts[0])
    day = int(parts[1])
    year = datetime.now().year

    result = date(year, month, day)

    # If the expiry is in the past, it likely refers to next year
    if result < date.today():
        result = date(year + 1, month, day)

    return result


def _parse_option_type(char: str) -> str:
    """Convert C/P to CALL/PUT."""
    return "CALL" if char.upper() == "C" else "PUT"


def parse_buy_line(line: str) -> Optional[Signal]:
    """Parse a single BUY signal line.

    Supports formats with and without expiry, with or without '@' before price,
    and with leading text/emojis before the keyword.

    Args:
        line: A line like "BOUGHT SPY 3/20 657P 2.29 lotto"
              or "BOUGHT SPY 657P @ 2.29"

    Returns:
        Signal object or None if the line doesn't match.
    """
    stripped = line.strip()

    # Try with-expiry pattern first (more specific), then without-expiry
    match = _BUY_PATTERN.search(stripped)
    if match and match.group("expiry"):
        try:
            expiry = _parse_expiry(match.group("expiry"))
            return Signal(
                action="BUY",
                ticker=match.group("ticker").upper(),
                expiry=expiry,
                strike=float(match.group("strike")),
                option_type=_parse_option_type(match.group("opt_type")),
                price=float(match.group("price")),
                note=match.group("note").strip() if match.group("note") else None,
                raw=stripped,
            )
        except (ValueError, AttributeError) as exc:
            logger.warning("Failed to parse BUY (with expiry) line: %r — %s", line, exc)

    # Fall back to no-expiry pattern
    match = _BUY_NO_EXPIRY_PATTERN.search(stripped)
    if not match:
        return None

    try:
        return Signal(
            action="BUY",
            ticker=match.group("ticker").upper(),
            expiry=None,
            strike=float(match.group("strike")),
            option_type=_parse_option_type(match.group("opt_type")),
            price=float(match.group("price")),
            note=match.group("note").strip() if match.group("note") else None,
            raw=stripped,
        )
    except (ValueError, AttributeError) as exc:
        logger.warning("Failed to parse BUY (no expiry) line: %r — %s", line, exc)
        return None


def parse_sell_line(line: str) -> Optional[Signal]:
    """Parse a single SELL signal line.

    Supports '@' before price and leading text/emojis before the keyword.

    Args:
        line: A line like "SOLD SPY 681C 3.00 1/4 position"

    Returns:
        Signal object or None if the line doesn't match.
    """
    match = _SELL_PATTERN.search(line.strip())
    if not match:
        return None

    try:
        size_raw = match.group("size").strip()
        return Signal(
            action="SELL",
            ticker=match.group("ticker").upper(),
            expiry=None,  # SELL signals don't include expiry
            strike=float(match.group("strike")),
            option_type=_parse_option_type(match.group("opt_type")),
            price=float(match.group("price")),
            size=size_raw,
            raw=line.strip(),
        )
    except (ValueError, AttributeError) as exc:
        logger.warning("Failed to parse SELL line: %r — %s", line, exc)
        return None


def parse_message(content: str) -> list[Signal]:
    """Parse a multi-line Discord message into a list of Signal objects.

    Handles messages with multiple BUY and/or SELL signals, one per line.

    Args:
        content: The full message text from Discord.

    Returns:
        List of parsed Signal objects.
    """
    signals: list[Signal] = []

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        signal: Optional[Signal] = None
        upper = line.upper()

        # Use 'in' instead of 'startswith' to handle leading emojis/formatting
        if "BOUGHT" in upper:
            signal = parse_buy_line(line)
        elif "SOLD" in upper:
            signal = parse_sell_line(line)

        if signal is not None:
            signals.append(signal)
            logger.info("Parsed signal: %s %s %.1f%s @ %.2f", signal.action, signal.ticker, signal.strike, signal.option_type[0], signal.price)
        elif _LOOSE_BUY.search(line) or _LOOSE_SELL.search(line):
            # Line looks like a signal but didn't match — this is a MISSED signal
            logger.warning("MISSED SIGNAL — line contains BOUGHT/SOLD but failed to parse: %r", line)
        elif line:
            logger.debug("Skipped non-signal line: %r", line)

    return signals


def parse_sell_size(size_str: str) -> Optional[float]:
    """Convert a sell size string to a fraction of the position.

    Examples:
        "1/4 position" -> 0.25
        "1/2 position" -> 0.5
        "1/8 position" -> 0.125
        "ALL OUT"       -> 1.0
        "ALL"           -> 1.0

    Args:
        size_str: The size portion of a SELL signal.

    Returns:
        Fraction as a float (0.0–1.0), or None if unparseable.
    """
    normalized = size_str.strip().upper()

    if "ALL" in normalized:
        return 1.0

    # Match fraction pattern like "1/4"
    frac_match = re.search(r"(\d+)\s*/\s*(\d+)", normalized)
    if frac_match:
        numerator = int(frac_match.group(1))
        denominator = int(frac_match.group(2))
        if denominator == 0:
            return None
        return numerator / denominator

    return None
