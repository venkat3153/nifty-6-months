# =============================================================================
#  BROKER INTEGRATION — Zerodha Kite Connect
#
#  This file handles:
#  - Login / token generation
#  - Placing BUY / SELL market orders
#  - Fetching live option LTP (last traded price)
#  - Fetching 1-minute OHLCV candles for strategy signals
#
#  Setup steps:
#  1. pip install kiteconnect
#  2. Go to https://developers.kite.trade → Create App
#  3. Copy your API key & secret into config.py
#  4. Run `python broker_kite.py --login` once each morning to get access token
# =============================================================================

import logging
import webbrowser
from datetime import datetime, timedelta, date

import config

logger = logging.getLogger(__name__)

# Try importing kiteconnect; if not installed, show clear message
try:
    from kiteconnect import KiteConnect, KiteTicker
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False
    logger.warning("kiteconnect not installed. Run: pip install kiteconnect")


# ── Paper Trade Order Simulator ───────────────────────────────────────────────

class PaperBroker:
    """
    Simulates order execution without real money.
    Used when config.PAPER_TRADE = True.
    """

    def __init__(self):
        self.orders  = []
        self.order_id = 1000

    def place_order(self, order: dict) -> str:
        oid = str(self.order_id)
        self.order_id += 1
        order['order_id'] = oid
        order['timestamp'] = datetime.now()
        self.orders.append(order)
        logger.info(f"[PAPER] Order placed: {order['action']} {order['quantity']} "
                    f"{order['symbol']} @ MARKET → ID {oid}")
        return oid

    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float:
        """Paper broker returns a dummy LTP — replaced by real data in live mode."""
        logger.debug(f"[PAPER] LTP requested for {symbol} — returning 100.0")
        return 100.0

    def get_positions(self) -> list:
        return []


# ── Live Kite Broker ──────────────────────────────────────────────────────────

class KiteBroker:
    """
    Wraps Zerodha KiteConnect for live trading.
    """

    def __init__(self):
        if not KITE_AVAILABLE:
            raise ImportError("Run: pip install kiteconnect")
        self.kite = KiteConnect(api_key=config.API_KEY)
        self._connected = False

    # ── Authentication ────────────────────────────────────────────────────────
    def login(self):
        """
        Step 1: Opens browser for Kite login.
        Step 2: Paste the `request_token` from the redirect URL.
        Call this ONCE each morning before running main.py.
        """
        login_url = self.kite.login_url()
        print(f"\nOpening Kite login page...")
        print(f"URL: {login_url}\n")
        webbrowser.open(login_url)

        request_token = input(
            "After login, copy the 'request_token' from the redirect URL\n"
            "(URL looks like: http://localhost/?request_token=XXXXX&action=login)\n"
            "Paste request_token here: "
        ).strip()

        session = self.kite.generate_session(request_token, api_secret=config.API_SECRET)
        access_token = session["access_token"]
        self.kite.set_access_token(access_token)
        self._connected = True

        # Save token to config so main.py can use it without re-login
        self._save_token(access_token)
        print(f"\nLogin successful! Access token saved.\n")
        return access_token

    def connect(self, access_token: str = None):
        """Use saved/provided access token (no browser needed)."""
        token = access_token or config.ACCESS_TOKEN
        if not token:
            raise ValueError("No access token. Run login() first.")
        self.kite.set_access_token(token)
        self._connected = True
        logger.info("Kite connected with access token.")

    def _save_token(self, token: str):
        """Write today's token to a local file for easy reuse."""
        with open("kite_token.txt", "w") as f:
            f.write(token)
        logger.info("Token saved to kite_token.txt")

    # ── Order Placement ───────────────────────────────────────────────────────
    def place_order(self, order: dict) -> str:
        """
        Place a market order.
        order = {"action": "BUY"/"SELL", "symbol": "...", "exchange": "NFO", "quantity": int}
        Returns order_id string.
        """
        self._ensure_connected()
        transaction = (
            self.kite.TRANSACTION_TYPE_BUY
            if order["action"] == "BUY"
            else self.kite.TRANSACTION_TYPE_SELL
        )
        order_id = self.kite.place_order(
            tradingsymbol    = order["symbol"],
            exchange         = order["exchange"],
            transaction_type = transaction,
            quantity         = order["quantity"],
            order_type       = self.kite.ORDER_TYPE_MARKET,
            product          = self.kite.PRODUCT_MIS,   # Intraday
            variety          = self.kite.VARIETY_REGULAR,
        )
        logger.info(f"Order placed: {order['action']} {order['quantity']} "
                    f"{order['symbol']} → ID {order_id}")
        return str(order_id)

    # ── Market Data ───────────────────────────────────────────────────────────
    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float:
        """Fetch last traded price of an option symbol."""
        self._ensure_connected()
        key = f"{exchange}:{symbol}"
        data = self.kite.ltp([key])
        return float(data[key]["last_price"])

    def get_spot(self) -> float:
        """Fetch current Nifty 50 spot price."""
        self._ensure_connected()
        data = self.kite.ltp(["NSE:NIFTY 50"])
        return float(data["NSE:NIFTY 50"]["last_price"])

    def get_1min_candles(self, symbol: str, exchange: str = "NFO",
                          from_dt: datetime = None, to_dt: datetime = None) -> list[dict]:
        """
        Fetch 1-minute OHLCV candles.
        Returns list of dicts: {time, open, high, low, close, volume}
        """
        self._ensure_connected()
        if from_dt is None:
            from_dt = datetime.now().replace(hour=9, minute=15, second=0)
        if to_dt is None:
            to_dt = datetime.now()

        # Need instrument token for historical data
        token = self._get_instrument_token(symbol, exchange)
        data = self.kite.historical_data(
            instrument_token = token,
            from_date        = from_dt,
            to_date          = to_dt,
            interval         = "minute",
        )
        return [
            {
                "time"  : candle["date"],
                "open"  : candle["open"],
                "high"  : candle["high"],
                "low"   : candle["low"],
                "close" : candle["close"],
                "volume": candle["volume"],
            }
            for candle in data
        ]

    def get_nifty_candles(self, from_dt: datetime = None, to_dt: datetime = None) -> list[dict]:
        """Fetch 1-min candles for Nifty 50 index (for ORB + VWAP calculation)."""
        return self.get_1min_candles("NIFTY 50", exchange="NSE",
                                      from_dt=from_dt, to_dt=to_dt)

    def _get_instrument_token(self, symbol: str, exchange: str) -> int:
        """Look up instrument token from symbol name."""
        instruments = self.kite.instruments(exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == symbol:
                return inst["instrument_token"]
        raise ValueError(f"Instrument not found: {exchange}:{symbol}")

    def get_positions(self) -> list:
        self._ensure_connected()
        return self.kite.positions().get("day", [])

    def _ensure_connected(self):
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() or login() first.")


# ── Factory ───────────────────────────────────────────────────────────────────

def get_broker():
    """Returns PaperBroker or KiteBroker based on config.PAPER_TRADE."""
    if config.PAPER_TRADE:
        logger.info("PAPER TRADE mode — no real orders will be placed.")
        return PaperBroker()
    else:
        logger.info("LIVE mode — real orders will be placed. Be careful!")
        broker = KiteBroker()
        # Load saved token if available
        try:
            with open("kite_token.txt") as f:
                token = f.read().strip()
            broker.connect(token)
        except FileNotFoundError:
            logger.warning("No saved token. Run: python broker_kite.py --login")
        return broker


# ── CLI: Generate Token ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        print("Starting Kite login flow...")
        broker = KiteBroker()
        token = broker.login()
        print(f"Token: {token}")
        print("You can now run: python main.py")
    else:
        print("Usage: python broker_kite.py --login")
