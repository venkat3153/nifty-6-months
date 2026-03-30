# =============================================================================
#  NIFTY 50 OPTIONS ALGO — CONFIGURATION
#  Edit ONLY this file to tune the strategy.
#  Do NOT touch strategy.py or main.py unless you know what you are doing.
# =============================================================================

# ── Capital & Risk ────────────────────────────────────────────────────────────
CAPITAL          = 150_000   # Your deployed capital in ₹ (1.5 Lakh)
RISK_PCT         = 1.0       # Max risk per trade as % of capital → ₹1,500
DAILY_LOSS_LIMIT = 1.5       # Stop algo if day loss hits this % → ₹2,250
DAILY_TARGET     = 2.0       # Stop algo after hitting this % profit → ₹3,000

# ── Nifty Options ─────────────────────────────────────────────────────────────
LOT_SIZE         = 25        # Nifty 50 current lot size (reduced Nov 2024)
MAX_LOTS         = 2         # Hard cap — never exceed this many lots
STRIKE_STEP      = 50        # Nifty strikes are in steps of 50
USE_WEEKLY       = True      # True = weekly expiry (better premium decay)
EXPIRY_WEEKDAY   = 3         # 3 = Thursday (NSE weekly expiry day)

# ── Strategy: Opening Range Breakout (ORB) + VWAP Filter ─────────────────────
ORB_MINUTES      = 15        # Capture first 15 min as opening range
MIN_ORB_RANGE    = 40        # Skip trade if ORB is too tight (< 40 pts)
MAX_ORB_RANGE    = 250       # Skip trade if ORB is too wide / volatile (> 250 pts)
BREAKOUT_BUFFER  = 5         # Extra points above/below ORB high/low to confirm break
VWAP_FILTER      = True      # Only trade in direction of VWAP (reduces false signals)

# ── Option Pricing ────────────────────────────────────────────────────────────
SL_PCT           = 40        # Stop loss: exit if premium falls 40% from entry
TARGET_PCT       = 80        # Target: exit if premium rises 80% from entry (1:2 R:R)
# Example: entry ₹100 → SL at ₹60, Target at ₹180
# Risk per lot  = (100 - 60)  × 25 = ₹1,000
# Reward per lot= (180 - 100) × 25 = ₹2,000

# ── Time Rules (IST, 24h format) ─────────────────────────────────────────────
ORB_START        = "09:15"   # Market open — start capturing range
ORB_END          = "09:30"   # Opening range window closes
TRADE_START      = "09:30"   # Look for breakout from here
NO_NEW_TRADES    = "14:00"   # Do not enter new positions after this time
SQUARE_OFF       = "15:10"   # Force-close ALL open positions at this time

# ── Broker Selection ──────────────────────────────────────────────────────────
# Choose ONE broker: "angel", "dhan", or "upstox"
BROKER = "angel"

# ── Angel One (SmartAPI) ──────────────────────────────────────────────────────
# Get from: https://smartapi.angelbroking.com → My Account → Enable API
# TOTP key: AngelOne app → My Profile → Enable TOTP → copy the secret key
ANGEL_API_KEY   = "your_angel_api_key"
ANGEL_CLIENT_ID = "your_client_id"        # Your Angel One login ID (e.g. A123456)
ANGEL_PASSWORD  = "your_login_password"
ANGEL_TOTP_KEY  = "your_totp_secret_key"  # 32-char base32 key shown when you enable TOTP

# ── Dhan ──────────────────────────────────────────────────────────────────────
# Get from: https://dhanhq.co → API Portal → Create App
# Token is valid for 30 days — no daily login needed
DHAN_CLIENT_ID    = "your_dhan_client_id"
DHAN_ACCESS_TOKEN = "your_dhan_access_token"

# ── Upstox ────────────────────────────────────────────────────────────────────
# Get from: https://developer.upstox.com → Create App
# Run: python broker.py --login  each morning to get fresh access token
UPSTOX_API_KEY      = "your_upstox_api_key"
UPSTOX_API_SECRET   = "your_upstox_api_secret"
UPSTOX_REDIRECT_URI = "http://127.0.0.1:8080/"
UPSTOX_ACCESS_TOKEN = ""   # Filled after daily login

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE     = "trade_log.csv"   # Every trade is recorded here
PAPER_TRADE  = True              # True = simulate orders, no real money
                                 # Set to False ONLY when ready to go live
