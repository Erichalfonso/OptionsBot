"""Configuration: loads environment variables and defines constants."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")

# Discord settings
DISCORD_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID: int = 854116314020970496
SIGNAL_AUTHOR: str = "grailedmund"

# Alpaca settings
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Trading settings
DEFAULT_QUANTITY: int = int(os.getenv("DEFAULT_QUANTITY", "1"))
MAX_POSITION_SIZE: int = int(os.getenv("MAX_POSITION_SIZE", "10"))  # hard cap contracts per trade

# Minimum buying power required to place a trade (buffer)
MIN_BUYING_POWER: float = float(os.getenv("MIN_BUYING_POWER", "500.0"))

# Risk Management — Position Sizing (percentage of account equity)
MIN_TRADE_PCT: float = float(os.getenv("MIN_TRADE_PCT", "1.0"))    # min 1% per trade
MAX_TRADE_PCT: float = float(os.getenv("MAX_TRADE_PCT", "5.0"))    # max 5% per trade

# Risk Management — Exposure Limits (percentage of account equity)
MAX_EXPOSURE_PCT: float = float(os.getenv("MAX_EXPOSURE_PCT", "8.0"))        # soft target
HARD_CAP_EXPOSURE_PCT: float = float(os.getenv("HARD_CAP_EXPOSURE_PCT", "10.0"))  # absolute max

# Email notifications (Gmail)
EMAIL_ADDRESS: str = os.getenv("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD: str = os.getenv("EMAIL_APP_PASSWORD", "")

# Database — use Railway volume mount if available, else local
_db_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", str(Path(__file__).parent))
DB_PATH: str = str(Path(_db_dir) / "trades.db")
