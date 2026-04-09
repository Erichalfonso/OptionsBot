"""Unit tests for on_message_edit dedup logic.

Tests the signal key generation and filtering that prevents double-trading
when Discord messages are edited.
"""

import pytest
from datetime import date

from parser import Signal, parse_message


# Import the dedup helpers from bot module
# We re-implement _signal_key here to avoid importing the full bot (which starts Discord)
def _signal_key(signal: Signal) -> str:
    """Hashable key for dedup: action|ticker|strike|option_type."""
    return f"{signal.action}|{signal.ticker}|{signal.strike}|{signal.option_type}"


class TestSignalKey:
    """Test _signal_key generates correct dedup keys."""

    def test_buy_key(self):
        sig = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "CALL", 3.00)
        assert _signal_key(sig) == "BUY|SPY|677.0|CALL"

    def test_sell_key(self):
        sig = Signal("SELL", "QQQ", None, 592.0, "CALL", 17.34, size="3/4 position")
        assert _signal_key(sig) == "SELL|QQQ|592.0|CALL"

    def test_different_strikes_are_different_keys(self):
        sig1 = Signal("BUY", "SPY", date(2026, 4, 10), 577.0, "CALL", 3.00)
        sig2 = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "CALL", 3.00)
        assert _signal_key(sig1) != _signal_key(sig2)

    def test_expiry_not_in_key(self):
        """Expiry is NOT part of the key — same ticker/strike/type should dedup."""
        sig1 = Signal("BUY", "SPY", date(2026, 4, 9), 677.0, "CALL", 3.00)
        sig2 = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "CALL", 3.00)
        assert _signal_key(sig1) == _signal_key(sig2)

    def test_price_not_in_key(self):
        """Price is NOT part of the key — same contract at different price should dedup."""
        sig1 = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "CALL", 3.00)
        sig2 = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "CALL", 3.50)
        assert _signal_key(sig1) == _signal_key(sig2)

    def test_put_vs_call_are_different(self):
        sig_call = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "CALL", 3.00)
        sig_put = Signal("BUY", "SPY", date(2026, 4, 10), 677.0, "PUT", 3.00)
        assert _signal_key(sig_call) != _signal_key(sig_put)


class TestEditFiltering:
    """Test the edit filtering logic that prevents double-trading."""

    def test_all_succeeded_skips_everything(self):
        """If all signals succeeded on original, edit should produce no new signals."""
        msg = "BOUGHT SPY 4/10 677C 3.00\nBOUGHT QQQ 4/10 607C 3.02"
        signals = parse_message(msg)
        assert len(signals) == 2

        # Simulate all succeeded
        prior = {_signal_key(s) for s in signals}

        # Reparse same content (cosmetic edit)
        new_signals = [s for s in parse_message(msg) if _signal_key(s) not in prior]
        assert new_signals == []

    def test_failed_signal_retried_on_edit(self):
        """If original had wrong strike, edited version with correct strike should execute."""
        original = "BOUGHT SPY 4/10 577C 3.00\nBOUGHT QQQ 4/10 607C 3.02"
        edited = "BOUGHT SPY 4/10 677C 3.00\nBOUGHT QQQ 4/10 607C 3.02"

        orig_signals = parse_message(original)
        assert len(orig_signals) == 2

        # Simulate: SPY 577C failed, QQQ 607C succeeded
        prior = set()
        # Only QQQ succeeded
        prior.add(_signal_key(orig_signals[1]))  # QQQ 607C

        # Reparse edited content
        edit_signals = parse_message(edited)
        new_signals = [s for s in edit_signals if _signal_key(s) not in prior]

        # Only SPY 677C should be new (577C key != 677C key)
        assert len(new_signals) == 1
        assert new_signals[0].ticker == "SPY"
        assert new_signals[0].strike == 677.0

    def test_nothing_succeeded_retries_all(self):
        """If original completely failed, all signals from edit should execute."""
        edited = "BOUGHT SPY 4/10 677C 3.00\nBOUGHT QQQ 4/10 607C 3.02"
        prior = set()  # nothing succeeded

        signals = parse_message(edited)
        new_signals = [s for s in signals if _signal_key(s) not in prior]
        assert len(new_signals) == 2

    def test_partial_success_only_retries_failed(self):
        """Multi-signal sell message where some succeeded and some failed."""
        msg = (
            "SOLD SPY 660C 17.00 ALL OUT\n"
            "SOLD QQQ 590C 19.50 ALL OUT\n"
            "SOLD SPY 661C 16.00 ALL OUT\n"
            "SOLD QQQ 591C 18.73 ALL OUT\n"
            "SOLD SPY 663C 14.49 3/4 position\n"
            "SOLD QQQ 592C 17.34 3/4 position"
        )
        signals = parse_message(msg)
        assert len(signals) == 6

        # Simulate: first 4 succeeded, last 2 failed
        prior = {_signal_key(s) for s in signals[:4]}

        new_signals = [s for s in parse_message(msg) if _signal_key(s) not in prior]
        assert len(new_signals) == 2
        assert new_signals[0].ticker == "SPY"
        assert new_signals[0].strike == 663.0
        assert new_signals[1].ticker == "QQQ"
        assert new_signals[1].strike == 592.0

    def test_unparseable_original_parsed_after_edit(self):
        """Original message had missing expiry (unparseable), edit adds it."""
        # Original: QQQ has no expiry — parser can't parse it
        original = "BOUGHT SPY 4/10 677C 3.00\nBOUGHT QQQ 607C 3.02"
        orig_signals = parse_message(original)
        # Only SPY parses (QQQ has no expiry)
        assert len(orig_signals) == 1
        assert orig_signals[0].ticker == "SPY"

        # SPY succeeded
        prior = {_signal_key(orig_signals[0])}

        # Edit adds expiry to QQQ
        edited = "BOUGHT SPY 4/10 677C 3.00\nBOUGHT QQQ 4/10 607C 3.02"
        edit_signals = parse_message(edited)
        new_signals = [s for s in edit_signals if _signal_key(s) not in prior]

        # Only QQQ should be new
        assert len(new_signals) == 1
        assert new_signals[0].ticker == "QQQ"
        assert new_signals[0].strike == 607.0

    def test_sell_missing_option_type_then_fixed(self):
        """Original sell missing C/P fails to parse, edit fixes it."""
        original = "SOLD QQQ 592 17.34 3/4 position"
        assert parse_message(original) == []

        edited = "SOLD QQQ 592C 17.34 3/4 position"
        signals = parse_message(edited)
        assert len(signals) == 1

        # No prior successes (original didn't parse)
        new_signals = [s for s in signals if _signal_key(s) not in set()]
        assert len(new_signals) == 1
        assert new_signals[0].ticker == "QQQ"


class TestRealWorldScenarios:
    """Test with actual signal formats from the Discord channel."""

    def test_april8_buy_edit_scenario(self):
        """Reproduce the exact April 8 failure: guy posted wrong strike, then edited."""
        # What the bot received originally
        original = "BOUGHT SPY 4/10 577C 3.00\nBOUGHT QQQ 607C 3.02"
        orig_signals = parse_message(original)
        # SPY parses (wrong strike), QQQ fails (no expiry)
        assert len(orig_signals) == 1

        # SPY 577C failed at broker — nothing in success set
        prior = set()

        # What Discord shows after edit
        edited = "BOUGHT SPY 4/10 677C 3.00\nBOUGHT QQQ 4/10 607C 3.02"
        edit_signals = parse_message(edited)
        assert len(edit_signals) == 2

        new_signals = [s for s in edit_signals if _signal_key(s) not in prior]
        # Both should execute
        assert len(new_signals) == 2
        assert new_signals[0].ticker == "SPY"
        assert new_signals[0].strike == 677.0
        assert new_signals[1].ticker == "QQQ"
        assert new_signals[1].strike == 607.0

    def test_april8_sell_scenario(self):
        """Reproduce April 8 sell: QQQ 592 missing C, then edited to 592C."""
        original = (
            "SOLD SPY 660C 17.00 ALL OUT\n"
            "SOLD QQQ 590C 19.50 ALL OUT\n"
            "SOLD SPY 661C 16.00 ALL OUT\n"
            "SOLD QQQ 591C 18.73 ALL OUT\n"
            "SOLD SPY 663C 14.49 3/4 position\n"
            "SOLD QQQ 592 17.34 3/4 position"  # missing C
        )
        orig_signals = parse_message(original)
        assert len(orig_signals) == 5  # QQQ 592 fails

        # All 5 failed because no Alpaca position, but that's a runtime issue.
        # For dedup purposes, none succeeded.
        prior = set()

        edited = (
            "SOLD SPY 660C 17.00 ALL OUT\n"
            "SOLD QQQ 590C 19.50 ALL OUT\n"
            "SOLD SPY 661C 16.00 ALL OUT\n"
            "SOLD QQQ 591C 18.73 ALL OUT\n"
            "SOLD SPY 663C 14.49 3/4 position\n"
            "SOLD QQQ 592C 17.34 3/4 position"  # C added
        )
        edit_signals = parse_message(edited)
        assert len(edit_signals) == 6

        new_signals = [s for s in edit_signals if _signal_key(s) not in prior]
        # All 6 should retry since none succeeded
        assert len(new_signals) == 6
