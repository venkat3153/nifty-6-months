# =============================================================================
#  BROKER INTEGRATION — Angel One | Dhan | Upstox
#
#  Supports all three brokers through a common interface.
#  Switch broker by setting config.BROKER = "angel" / "dhan" / "upstox"
#
#  Common interface (all brokers implement):
#    login()                          → authenticate, save token
#    place_order(order_dict)          → returns order_id
#    get_ltp(symbol, exchange)        → returns float (last traded price)
#    get_spot()                       → returns Nifty 50 spot float
#    get_nifty_candles(from_dt,to_dt) → returns list of OHLCV dicts
#    get_option_token(strike, type, expiry) → broker-specific instrument ID
#
#  Setup per broker:
#  ─────────────────────────────────────────────────────────────────────────────
#  Angel One:
#    pip install smartapi-python pyotp requests websocket-client
#    Fill ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_KEY
#    No daily login needed — TOTP auto-generates on each run
#
#  Dhan:
#    pip install dhanhq
#    Fill DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN
#    Token is valid 30 days — no daily login needed
#
#  Upstox:
#    pip install upstox-python-sdk
#    Fill UPSTOX_API_KEY, UPSTOX_API_SECRET
#    Run: python broker.py --login   (once every day before 9:15 AM)
# =============================================================================

import os
import sys
import csv
import json
import logging
import webbrowser
import urllib.parse
import http.server
import threading
import time as time_module
from datetime import datetime, timedelta, date
from io import StringIO

import config

logger = logging.getLogger(__name__)


# =============================================================================
#  PAPER BROKER — simulates orders, no real money
# =============================================================================

class PaperBroker:
    """Used when config.PAPER_TRADE = True."""

    def __init__(self):
        self._order_id = 1000

    def login(self):
        logger.info("[PAPER] Login skipped — paper trade mode")

    def place_order(self, order: dict) -> str:
        oid = str(self._order_id)
        self._order_id += 1
        logger.info(f"[PAPER] {order['action']} {order['quantity']} "
                    f"{order['symbol']} @ MARKET → ID {oid}")
        return oid

    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float:
        return 100.0   # dummy — replaced by real simulation in main.py

    def get_spot(self) -> float:
        return 24000.0

    def get_nifty_candles(self, from_dt=None, to_dt=None) -> list:
        return []

    def get_option_token(self, strike: int, option_type: str, expiry: date) -> str:
        return f"PAPER_{strike}{option_type}"


# =============================================================================
#  ANGEL ONE BROKER  (SmartAPI)
# =============================================================================

class AngelBroker:
    """
    Uses Angel One SmartAPI.
    TOTP is auto-generated every run — no manual daily login needed.

    pip install smartapi-python pyotp requests websocket-client
    """

    # Angel One instrument master URL
    SCRIP_MASTER_URL = (
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    def __init__(self):
        self._check_deps()
        from SmartApi import SmartConnect
        self.obj = SmartConnect(api_key=config.ANGEL_API_KEY)
        self._token_map: dict = {}   # symbol → symboltoken cache
        self._auth_token: str = ""

    def _check_deps(self):
        try:
            import SmartApi   # noqa
            import pyotp      # noqa
        except ImportError:
            raise ImportError(
                "Run: pip install smartapi-python pyotp requests websocket-client"
            )

    # ── Auth ─────────────────────────────────────────────────────────────────
    def login(self):
        import pyotp
        totp = pyotp.TOTP(config.ANGEL_TOTP_KEY).now()
        data = self.obj.generateSession(
            config.ANGEL_CLIENT_ID,
            config.ANGEL_PASSWORD,
            totp,
        )
        if data["status"] is False:
            raise RuntimeError(f"Angel login failed: {data['message']}")
        self._auth_token = data["data"]["jwtToken"]
        logger.info("Angel One login successful.")

        # Load instrument master into cache
        self._load_instrument_master()

    def _load_instrument_master(self):
        """Download NFO instrument master and build symbol→token map."""
        cache_file = "angel_instruments.json"

        # Reuse today's cache if it exists
        if os.path.exists(cache_file):
            file_date = datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
            if file_date == date.today():
                with open(cache_file) as f:
                    self._token_map = json.load(f)
                logger.info(f"Angel instrument cache loaded ({len(self._token_map)} NFO symbols)")
                return

        logger.info("Downloading Angel One instrument master...")
        import requests
        resp = requests.get(self.SCRIP_MASTER_URL, timeout=30)
        resp.raise_for_status()
        instruments = resp.json()

        self._token_map = {
            inst["symbol"]: inst["token"]
            for inst in instruments
            if inst.get("exch_seg") == "NFO"
        }
        with open(cache_file, "w") as f:
            json.dump(self._token_map, f)
        logger.info(f"Angel instruments cached ({len(self._token_map)} NFO symbols)")

    def get_option_token(self, strike: int, option_type: str, expiry: date) -> str:
        """
        Returns Angel One symboltoken for an NFO option.
        Symbol format: NIFTY24APR2524000CE
        """
        expiry_str = expiry.strftime("%d%b%y").upper()
        symbol = f"NIFTY{expiry_str}{strike}{option_type}"
        token = self._token_map.get(symbol)
        if not token:
            raise ValueError(
                f"Token not found for {symbol}. "
                f"Check if expiry is correct or refresh instrument master."
            )
        return token

    # ── Orders ───────────────────────────────────────────────────────────────
    def place_order(self, order: dict) -> str:
        token = self._token_map.get(order["symbol"], "0")
        params = {
            "variety"        : "NORMAL",
            "tradingsymbol"  : order["symbol"],
            "symboltoken"    : token,
            "transactiontype": order["action"],          # "BUY" / "SELL"
            "exchange"       : order.get("exchange", "NFO"),
            "ordertype"      : "MARKET",
            "producttype"    : "INTRADAY",
            "duration"       : "DAY",
            "quantity"       : str(order["quantity"]),
            "price"          : "0",
            "triggerprice"   : "0",
        }
        resp = self.obj.placeOrder(params)
        order_id = resp.get("data", {}).get("orderid", "UNKNOWN")
        logger.info(f"Angel order placed: {order['action']} {order['quantity']} "
                    f"{order['symbol']} → ID {order_id}")
        return str(order_id)

    # ── Market Data ───────────────────────────────────────────────────────────
    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float:
        token = self._token_map.get(symbol, "0")
        resp = self.obj.ltpData(exchange, symbol, token)
        return float(resp["data"]["ltp"])

    def get_spot(self) -> float:
        resp = self.obj.ltpData("NSE", "Nifty 50", "99926000")
        return float(resp["data"]["ltp"])

    def get_nifty_candles(self, from_dt: datetime = None, to_dt: datetime = None) -> list:
        """Fetch 1-minute candles for Nifty 50 index."""
        if from_dt is None:
            from_dt = datetime.now().replace(hour=9, minute=15, second=0)
        if to_dt is None:
            to_dt = datetime.now()
        params = {
            "exchange"  : "NSE",
            "symboltoken": "99926000",    # Nifty 50 index token
            "interval"  : "ONE_MINUTE",
            "fromdate"  : from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate"    : to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        resp = self.obj.getCandleData(params)
        candles = []
        for row in (resp.get("data") or []):
            # row = [timestamp, open, high, low, close, volume]
            candles.append({
                "time"  : datetime.strptime(row[0][:19], "%Y-%m-%dT%H:%M:%S"),
                "open"  : float(row[1]),
                "high"  : float(row[2]),
                "low"   : float(row[3]),
                "close" : float(row[4]),
                "volume": float(row[5]),
            })
        return candles


# =============================================================================
#  DHAN BROKER
# =============================================================================

class DhanBroker:
    """
    Uses Dhan HQ API.
    Token is valid for 30 days — set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in config.py.
    No daily login needed.

    pip install dhanhq
    """

    # Dhan instrument master CSV (download once)
    SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

    def __init__(self):
        self._check_deps()
        from dhanhq import dhanhq
        self.dhan = dhanhq(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN)
        self._sec_id_map: dict = {}   # symbol → security_id

    def _check_deps(self):
        try:
            import dhanhq  # noqa
        except ImportError:
            raise ImportError("Run: pip install dhanhq")

    def login(self):
        """Dhan uses a long-lived token — no browser login needed."""
        self._load_instrument_master()
        logger.info("Dhan connected (no login required — using access token).")

    def _load_instrument_master(self):
        cache_file = "dhan_instruments.csv"
        if os.path.exists(cache_file):
            file_date = datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
            if file_date == date.today():
                self._build_sec_id_map(cache_file)
                return

        logger.info("Downloading Dhan instrument master...")
        import requests
        resp = requests.get(self.SCRIP_MASTER_URL, timeout=30)
        resp.raise_for_status()
        with open(cache_file, "w") as f:
            f.write(resp.text)
        self._build_sec_id_map(cache_file)

    def _build_sec_id_map(self, filepath: str):
        """
        Build a map: trading_symbol → security_id for NFO instruments.
        Dhan CSV columns include: SEM_TRADING_SYMBOL, SEM_SMST_SECURITY_ID, SEM_EXM_EXCH_ID
        """
        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("SEM_EXM_EXCH_ID") == "NFO":
                    sym = row.get("SEM_TRADING_SYMBOL", "").strip()
                    sid = row.get("SEM_SMST_SECURITY_ID", "").strip()
                    if sym and sid:
                        self._sec_id_map[sym] = sid
        logger.info(f"Dhan instruments loaded ({len(self._sec_id_map)} NFO symbols)")

    def get_option_token(self, strike: int, option_type: str, expiry: date) -> str:
        """Returns Dhan security_id for an NFO option."""
        expiry_str = expiry.strftime("%d%b%y").upper()
        symbol = f"NIFTY{expiry_str}{strike}{option_type}"
        sec_id = self._sec_id_map.get(symbol)
        if not sec_id:
            raise ValueError(f"Security ID not found for {symbol}")
        return sec_id

    # ── Orders ───────────────────────────────────────────────────────────────
    def place_order(self, order: dict) -> str:
        from dhanhq import dhanhq as dh
        sec_id = self._sec_id_map.get(order["symbol"], "")
        txn = dh.BUY if order["action"] == "BUY" else dh.SELL
        resp = self.dhan.place_order(
            security_id      = sec_id,
            exchange_segment = self.dhan.NSE_FO,
            transaction_type = txn,
            quantity         = order["quantity"],
            order_type       = self.dhan.MARKET,
            product_type     = self.dhan.INTRA,
            price            = 0,
        )
        order_id = str(resp.get("data", {}).get("orderId", "UNKNOWN"))
        logger.info(f"Dhan order: {order['action']} {order['quantity']} "
                    f"{order['symbol']} → ID {order_id}")
        return order_id

    # ── Market Data ───────────────────────────────────────────────────────────
    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float:
        sec_id = self._sec_id_map.get(symbol, "")
        resp = self.dhan.get_market_feed_quote(
            self.dhan.NSE_FO,
            sec_id,
        )
        return float(resp["data"]["lastTradedPrice"])

    def get_spot(self) -> float:
        # Nifty 50 index in Dhan uses NSE exchange, security_id = "13"
        resp = self.dhan.get_market_feed_quote(self.dhan.IDX, "13")
        return float(resp["data"]["lastTradedPrice"])

    def get_nifty_candles(self, from_dt: datetime = None, to_dt: datetime = None) -> list:
        """Fetch 1-minute candles for Nifty 50 via Dhan intraday data."""
        if from_dt is None:
            from_dt = datetime.now().replace(hour=9, minute=15, second=0)
        resp = self.dhan.intraday_minute_data(
            security_id      = "13",
            exchange_segment = self.dhan.IDX,
            instrument_type  = "INDEX",
        )
        candles = []
        for row in (resp.get("data", {}).get("open", [])):
            # Dhan returns separate arrays per OHLCV field
            pass
        # Dhan's intraday_minute_data returns a different structure — parse correctly:
        data = resp.get("data") or {}
        timestamps = data.get("timestamp", [])
        opens      = data.get("open", [])
        highs      = data.get("high", [])
        lows       = data.get("low", [])
        closes     = data.get("close", [])
        volumes    = data.get("volume", [])
        for i, ts in enumerate(timestamps):
            try:
                candles.append({
                    "time"  : datetime.fromtimestamp(ts),
                    "open"  : float(opens[i]),
                    "high"  : float(highs[i]),
                    "low"   : float(lows[i]),
                    "close" : float(closes[i]),
                    "volume": float(volumes[i]) if i < len(volumes) else 0.0,
                })
            except (IndexError, ValueError):
                continue
        return candles


# =============================================================================
#  UPSTOX BROKER
# =============================================================================

class UpstoxBroker:
    """
    Uses Upstox API v2.
    Requires daily OAuth login — run:  python broker.py --login

    pip install upstox-python-sdk
    """

    INSTRUMENT_URL = (
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    )
    NFO_INSTRUMENT_URL = (
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE_FO.json.gz"
    )

    def __init__(self):
        self._check_deps()
        self._access_token = config.UPSTOX_ACCESS_TOKEN
        self._inst_map: dict = {}   # symbol → instrument_key

    def _check_deps(self):
        try:
            import upstox_client  # noqa
        except ImportError:
            raise ImportError("Run: pip install upstox-python-sdk")

    # ── Auth ─────────────────────────────────────────────────────────────────
    def login(self):
        """
        OAuth2 login flow.
        Opens browser → you approve → redirected to localhost → token captured.
        """
        import upstox_client

        auth_url = (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code"
            f"&client_id={config.UPSTOX_API_KEY}"
            f"&redirect_uri={urllib.parse.quote(config.UPSTOX_REDIRECT_URI)}"
        )
        code_holder = []

        # Start local HTTP server to catch the redirect
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if code:
                    code_holder.append(code)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Login successful. You can close this tab.</h2>")

            def log_message(self, *args):
                pass

        port = int(config.UPSTOX_REDIRECT_URI.split(":")[-1].rstrip("/"))
        server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        print(f"\nOpening Upstox login page...")
        webbrowser.open(auth_url)
        thread.join(timeout=120)
        server.server_close()

        if not code_holder:
            raise RuntimeError("Upstox login timed out. Try again.")

        # Exchange code for access token
        import requests
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={
                "code"         : code_holder[0],
                "client_id"    : config.UPSTOX_API_KEY,
                "client_secret": config.UPSTOX_API_SECRET,
                "redirect_uri" : config.UPSTOX_REDIRECT_URI,
                "grant_type"   : "authorization_code",
            },
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        self._save_token(self._access_token)
        logger.info("Upstox login successful.")
        self._load_instrument_master()

    def _save_token(self, token: str):
        with open("upstox_token.txt", "w") as f:
            f.write(token)

    def _load_token(self):
        try:
            with open("upstox_token.txt") as f:
                self._access_token = f.read().strip()
        except FileNotFoundError:
            raise RuntimeError("No Upstox token. Run: python broker.py --login")

    def _load_instrument_master(self):
        """Download NFO instrument master and build symbol→instrument_key map."""
        import gzip
        import requests
        cache_file = "upstox_instruments.json"

        if os.path.exists(cache_file):
            file_date = datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
            if file_date == date.today():
                with open(cache_file) as f:
                    self._inst_map = json.load(f)
                logger.info(f"Upstox instrument cache loaded ({len(self._inst_map)} NFO symbols)")
                return

        logger.info("Downloading Upstox NFO instrument master...")
        resp = requests.get(self.NFO_INSTRUMENT_URL, timeout=60)
        resp.raise_for_status()
        instruments = json.loads(gzip.decompress(resp.content))

        self._inst_map = {}
        for inst in instruments:
            ts  = inst.get("trading_symbol", "")
            key = inst.get("instrument_key", "")
            if ts and key:
                self._inst_map[ts] = key

        with open(cache_file, "w") as f:
            json.dump(self._inst_map, f)
        logger.info(f"Upstox instruments cached ({len(self._inst_map)} NFO symbols)")

    def connect(self):
        """Load saved token (no browser). Call after daily login."""
        self._load_token()
        self._load_instrument_master()
        logger.info("Upstox connected with saved token.")

    def get_option_token(self, strike: int, option_type: str, expiry: date) -> str:
        """Returns Upstox instrument_key for an NFO option."""
        expiry_str = expiry.strftime("%d%b%y").upper()
        symbol = f"NIFTY{expiry_str}{strike}{option_type}"
        key = self._inst_map.get(symbol)
        if not key:
            raise ValueError(f"Instrument key not found for {symbol}")
        return key

    def _api_config(self):
        import upstox_client
        cfg = upstox_client.Configuration()
        cfg.access_token = self._access_token
        return cfg

    # ── Orders ───────────────────────────────────────────────────────────────
    def place_order(self, order: dict) -> str:
        import upstox_client
        instrument_key = self._inst_map.get(order["symbol"], "")
        api = upstox_client.OrderApi(upstox_client.ApiClient(self._api_config()))
        body = upstox_client.PlaceOrderRequest(
            quantity          = order["quantity"],
            product           = "I",        # Intraday
            validity          = "DAY",
            price             = 0,
            tag               = "algo",
            instrument_token  = instrument_key,
            order_type        = "MARKET",
            transaction_type  = order["action"],
            disclosed_quantity= 0,
            trigger_price     = 0,
            is_amo            = False,
        )
        resp = api.place_order(body, api_version="2.0")
        order_id = str(resp.data.order_id)
        logger.info(f"Upstox order: {order['action']} {order['quantity']} "
                    f"{order['symbol']} → ID {order_id}")
        return order_id

    # ── Market Data ───────────────────────────────────────────────────────────
    def get_ltp(self, symbol: str, exchange: str = "NFO") -> float:
        import upstox_client
        inst_key = self._inst_map.get(symbol, "")
        api = upstox_client.MarketQuoteApi(upstox_client.ApiClient(self._api_config()))
        resp = api.ltp(inst_key, api_version="2.0")
        return float(list(resp.data.values())[0].last_price)

    def get_spot(self) -> float:
        import upstox_client
        api = upstox_client.MarketQuoteApi(upstox_client.ApiClient(self._api_config()))
        resp = api.ltp("NSE_INDEX|Nifty 50", api_version="2.0")
        return float(list(resp.data.values())[0].last_price)

    def get_nifty_candles(self, from_dt: datetime = None, to_dt: datetime = None) -> list:
        import upstox_client
        if from_dt is None:
            from_dt = datetime.now().replace(hour=9, minute=15, second=0)
        if to_dt is None:
            to_dt = datetime.now()
        api = upstox_client.HistoryApi(upstox_client.ApiClient(self._api_config()))
        resp = api.get_intra_day_candle_data(
            instrument_key = "NSE_INDEX|Nifty 50",
            interval       = "1minute",
            api_version    = "2.0",
        )
        candles = []
        for row in (resp.data or []):
            ts = datetime.strptime(row[0][:19], "%Y-%m-%dT%H:%M:%S")
            if from_dt <= ts <= to_dt:
                candles.append({
                    "time"  : ts,
                    "open"  : float(row[1]),
                    "high"  : float(row[2]),
                    "low"   : float(row[3]),
                    "close" : float(row[4]),
                    "volume": float(row[5]),
                })
        return candles


# =============================================================================
#  FACTORY
# =============================================================================

def get_broker():
    """
    Returns the correct broker instance based on config.BROKER.
    Always returns PaperBroker when config.PAPER_TRADE = True.
    """
    if config.PAPER_TRADE:
        logger.info("PAPER TRADE mode — no real orders will be placed.")
        return PaperBroker()

    broker_name = config.BROKER.lower().strip()

    if broker_name == "angel":
        logger.info("Using broker: Angel One (SmartAPI)")
        b = AngelBroker()
        b.login()
        return b

    elif broker_name == "dhan":
        logger.info("Using broker: Dhan HQ")
        b = DhanBroker()
        b.login()
        return b

    elif broker_name == "upstox":
        logger.info("Using broker: Upstox")
        b = UpstoxBroker()
        b.connect()    # uses saved token from broker.py --login
        return b

    else:
        raise ValueError(
            f"Unknown broker: '{broker_name}'. "
            f"Set config.BROKER to 'angel', 'dhan', or 'upstox'."
        )


# =============================================================================
#  CLI — run once each morning for brokers that need daily login
#  Usage: python broker.py --login
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if "--login" not in sys.argv:
        print("Usage: python broker.py --login")
        print("  Generates/refreshes access token for your selected broker.")
        sys.exit(0)

    broker_name = config.BROKER.lower()
    print(f"\nStarting login for broker: {broker_name.upper()}")

    if broker_name == "angel":
        b = AngelBroker()
        b.login()
        print("Angel One: login successful — token auto-refreshed via TOTP.")
        print("You can now run: python main.py")

    elif broker_name == "dhan":
        print("Dhan uses a long-lived token (30 days).")
        print("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in config.py.")
        print("Get your token from: https://dhanhq.co → API Portal → Access Token")

    elif broker_name == "upstox":
        b = UpstoxBroker()
        b.login()
        print("Upstox: login successful — token saved to upstox_token.txt")
        print("You can now run: python main.py")

    else:
        print(f"Unknown broker: {broker_name}")
