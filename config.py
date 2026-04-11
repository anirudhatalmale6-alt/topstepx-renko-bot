"""Configuration for TopstepX Renko Alignment Bot"""

# TopstepX API credentials (set via environment or here)
API_KEY = ""
USERNAME = ""

# API URLs
REST_BASE = "https://api.thefuturesdesk.projectx.com"
MARKET_HUB = "https://rtc.thefuturesdesk.projectx.com/hubs/market"
USER_HUB = "https://rtc.thefuturesdesk.projectx.com/hubs/user"

# Trading session (Eastern Time)
SESSION_START = "09:27"
SESSION_END = "16:00"
TIMEZONE = "America/New_York"

# Order quantity
QTY = 1

# Contract symbol to trade (will be resolved to contract ID on startup)
SYMBOL = "NQ"  # or "ES"

# Renko brick sizes (points) - per timeframe
# These should match what you use on TradingView
RENKO_BRICK_SIZES = {
    "1m": None,   # Will be auto-calculated or set manually
    "3m": None,
    "5m": None,
    "15m": None,
}
