# =============================================================================
#  STRATEGY ENGINE — ORB + VWAP Filter
#
#  How the strategy works:
#  1. 9:15–9:30 → Record the High and Low (Opening Range)
#  2. 9:30 onwards → Wait for price to break out above/below the range
#  3. On breakout, check VWAP direction (filter to reduce false signals)
#  4. Enter 1 lot of ATM Call (breakout up) or ATM Put (breakout down)
#  5. Exit when: SL hit | Target hit | 3:10 PM | Daily target/loss reached
#  6. ONE trade per day — no revenge trading, no overtrading
# =============================================================================

import math
import logging
from datetime import datetime, time, timedelta, date
import pandas as pd

import config

logger = logging.getLogger(__name__)


# ── Utility: Strike Rounding ──────────────────────────────────────────────────

def get_atm_strike(spot_price: float) -> int:
    """Round spot price to nearest Nifty strike (multiples of 50)."""
    step = config.STRIKE_STEP
    return int(round(spot_price / step) * step)


def get_expiry_date(reference_date: date = None) -> date:
    """
    Returns the nearest upcoming Thursday (weekly expiry).
    If today is Thursday after market close, returns next Thursday.
    """
    if reference_date is None:
        reference_date = date.today()
    days_ahead = config.EXPIRY_WEEKDAY - reference_date.weekday()
    if days_ahead < 0:          # passed this week's Thursday
        days_ahead += 7
    elif days_ahead == 0:       # today is Thursday
        now = datetime.now().time()
        if now > time(15, 30):  # after market close
            days_ahead = 7
    return reference_date + timedelta(days=days_ahead)


def format_option_symbol(strike: int, option_type: str, expiry: date) -> str:
    """
    Build the NSE/Kite option symbol.
    Format: NIFTY + DDMMMYY + STRIKE + CE/PE
    Example: NIFTY25APR2524000CE
    """
    expiry_str = expiry.strftime("%d%b%y").upper()   # e.g. 24APR25
    return f"NIFTY{expiry_str}{strike}{option_type}"


# ── Position Sizer ────────────────────────────────────────────────────────────

def calculate_lots(current_capital: float, entry_premium: float) -> int:
    """
    How many lots can we trade without risking more than RISK_PCT of capital?

    Risk per lot = (entry_premium × SL_PCT/100) × LOT_SIZE
    Max lots     = (capital × RISK_PCT/100) / risk_per_lot
    """
    max_risk     = current_capital * config.RISK_PCT / 100
    risk_per_unit = entry_premium * config.SL_PCT / 100
    risk_per_lot  = risk_per_unit * config.LOT_SIZE
    if risk_per_lot <= 0:
        return 1
    lots = max(1, math.floor(max_risk / risk_per_lot))
    return min(lots, config.MAX_LOTS)


# ── VWAP Calculator ───────────────────────────────────────────────────────────

class VWAPCalculator:
    """Rolling intraday VWAP — resets every day at 9:15."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._cum_vol       = 0.0
        self._cum_price_vol = 0.0
        self.value          = None

    def update(self, high: float, low: float, close: float, volume: float):
        typical_price        = (high + low + close) / 3.0
        self._cum_price_vol += typical_price * volume
        self._cum_vol       += volume
        if self._cum_vol > 0:
            self.value = self._cum_price_vol / self._cum_vol
        return self.value


# ── Opening Range Tracker ─────────────────────────────────────────────────────

class OpeningRange:
    """Captures first ORB_MINUTES of trading as the Opening Range."""

    def __init__(self):
        self.high   = None
        self.low    = None
        self.locked = False   # True once ORB window closes

    def update(self, high: float, low: float, candle_time: time):
        if self.locked:
            return
        orb_end = time(*map(int, config.ORB_END.split(":")))
        if candle_time < orb_end:
            self.high = max(self.high, high) if self.high else high
            self.low  = min(self.low, low)   if self.low  else low
        else:
            self.locked = True

    @property
    def range_size(self) -> float:
        if self.high and self.low:
            return self.high - self.low
        return 0.0

    def is_valid(self) -> bool:
        """Reject ORB that is too tight or too wide."""
        r = self.range_size
        return self.locked and config.MIN_ORB_RANGE <= r <= config.MAX_ORB_RANGE

    def breakout_direction(self, price: float) -> str | None:
        """
        Returns 'CE' (bullish breakout), 'PE' (bearish breakdown), or None.
        Adds a small buffer to avoid false breakouts on the exact ORB line.
        """
        if not self.is_valid():
            return None
        buf = config.BREAKOUT_BUFFER
        if price > self.high + buf:
            return "CE"
        if price < self.low - buf:
            return "PE"
        return None


# ── Trade State Machine ───────────────────────────────────────────────────────

class TradeState:
    IDLE       = "IDLE"
    IN_TRADE   = "IN_TRADE"
    DONE       = "DONE"     # completed trade (win/loss)
    DAY_OVER   = "DAY_OVER" # daily target/loss hit — no more trades


class NiftyORBStrategy:
    """
    Complete strategy state machine.
    Call `on_tick()` every time you receive a new 1-min candle.
    When in a trade, also pass `option_ltp` (the live option premium)
    so SL/target checks work on the actual option price, not spot.
    Emits order signals via returned dicts — broker.py executes them.
    """

    def __init__(self):
        self.orb          = OpeningRange()
        self.vwap         = VWAPCalculator()
        self.state        = TradeState.IDLE
        self.trade_count  = 0          # max 1 per day
        self.day_pnl      = 0.0        # running P&L in ₹ for today
        self.current_cap  = float(config.CAPITAL)
        self.position     = None       # dict when in trade
        self._date        = date.today()

    # ── Called every new trading day ──────────────────────────────────────────
    def reset_for_new_day(self):
        logger.info("=" * 60)
        logger.info(f"New trading day: {date.today()}")
        logger.info(f"Capital: ₹{self.current_cap:,.0f}")
        logger.info("=" * 60)
        self.orb         = OpeningRange()
        self.vwap        = VWAPCalculator()
        self.state       = TradeState.IDLE
        self.trade_count = 0
        self.day_pnl     = 0.0
        self.position    = None
        self._date       = date.today()

    # ── Main tick handler ─────────────────────────────────────────────────────
    def on_tick(self, tick: dict, option_ltp: float = None) -> dict | None:
        """
        Called on every 1-minute candle close.
        tick = {
            'time'  : datetime,
            'open'  : float,
            'high'  : float,
            'low'   : float,
            'close' : float,   ← Nifty SPOT price
            'volume': float,
        }
        option_ltp: Live option premium (required when IN_TRADE for SL/target checks).
                    Broker fetches this separately and passes it here.
        Returns an order dict or None.
        """
        now   = tick['time'].time()
        price = tick['close']   # spot price — used for ORB/VWAP signals only

        # New day check
        if tick['time'].date() != self._date:
            self.reset_for_new_day()

        # Update VWAP with each candle
        self.vwap.update(tick['high'], tick['low'], price, tick['volume'])

        # Update opening range during 9:15–9:30
        self.orb.update(tick['high'], tick['low'], now)

        # ── Guard: stop trading conditions ────────────────────────────────────
        if self.state == TradeState.DAY_OVER:
            return None

        sq_off_time = time(*map(int, config.SQUARE_OFF.split(":")))
        no_new_time = time(*map(int, config.NO_NEW_TRADES.split(":")))

        # Force square-off at 3:10 PM
        if now >= sq_off_time and self.state == TradeState.IN_TRADE:
            return self._exit_trade("SQUARE_OFF", price)

        # ── IN_TRADE: check SL / Target using OPTION LTP ─────────────────────
        if self.state == TradeState.IN_TRADE:
            if option_ltp is not None:
                return self._check_exit(option_ltp)
            return None   # waiting for option LTP from broker

        # ── IDLE: look for entry ──────────────────────────────────────────────
        if self.state == TradeState.IDLE:
            if self.trade_count >= 1:   # only 1 trade per day
                return None
            if now >= no_new_time:       # too late in the day
                return None
            if not self.orb.locked:      # ORB still forming
                return None

            direction = self.orb.breakout_direction(price)
            if direction is None:
                return None

            # VWAP filter — trade only in direction of VWAP
            if config.VWAP_FILTER and self.vwap.value:
                if direction == "CE" and price < self.vwap.value:
                    logger.info(f"VWAP filter: skip CE (price {price:.0f} < VWAP {self.vwap.value:.0f})")
                    return None
                if direction == "PE" and price > self.vwap.value:
                    logger.info(f"VWAP filter: skip PE (price {price:.0f} > VWAP {self.vwap.value:.0f})")
                    return None

            return self._enter_trade(direction, price, tick['time'])

        return None

    # ── Entry ─────────────────────────────────────────────────────────────────
    def _enter_trade(self, option_type: str, spot: float, entry_time: datetime) -> dict:
        strike        = get_atm_strike(spot)
        expiry        = get_expiry_date(entry_time.date())
        symbol        = format_option_symbol(strike, option_type, expiry)
        lots          = calculate_lots(self.current_cap, entry_premium=100)  # placeholder
        quantity      = lots * config.LOT_SIZE

        self.position = {
            "symbol"     : symbol,
            "option_type": option_type,
            "strike"     : strike,
            "expiry"     : expiry,
            "lots"       : lots,
            "quantity"   : quantity,
            "entry_time" : entry_time,
            "entry_spot" : spot,
            "entry_prem" : None,   # filled after order execution (market order)
            "sl_prem"    : None,
            "tgt_prem"   : None,
        }
        self.state        = TradeState.IN_TRADE
        self.trade_count += 1

        logger.info(f"ENTRY SIGNAL → {option_type} | Strike: {strike} | Lots: {lots} | "
                    f"Symbol: {symbol} | Spot: {spot:.0f}")

        return {
            "action"  : "BUY",
            "symbol"  : symbol,
            "exchange": "NFO",
            "quantity": quantity,
            "order_type": "MARKET",
        }

    def set_entry_premium(self, premium: float):
        """
        Called by broker.py after the BUY order fills.
        Sets the actual entry premium, SL, and target.
        """
        if self.position is None:
            return
        sl_prem  = round(premium * (1 - config.SL_PCT  / 100), 1)
        tgt_prem = round(premium * (1 + config.TARGET_PCT / 100), 1)
        self.position['entry_prem'] = premium
        self.position['sl_prem']    = sl_prem
        self.position['tgt_prem']   = tgt_prem

        risk_per_lot   = (premium - sl_prem)  * config.LOT_SIZE
        reward_per_lot = (tgt_prem - premium) * config.LOT_SIZE
        total_risk     = risk_per_lot   * self.position['lots']
        total_reward   = reward_per_lot * self.position['lots']

        logger.info(f"Entry confirmed: premium ₹{premium} | SL ₹{sl_prem} | Target ₹{tgt_prem}")
        logger.info(f"Max loss: ₹{total_risk:.0f} | Max gain: ₹{total_reward:.0f}")

    # ── Exit logic ────────────────────────────────────────────────────────────
    def _check_exit(self, current_premium: float) -> dict | None:
        """Check if current premium hit SL or target."""
        if self.position is None or self.position['entry_prem'] is None:
            return None   # waiting for fill confirmation

        sl  = self.position['sl_prem']
        tgt = self.position['tgt_prem']

        if current_premium <= sl:
            return self._exit_trade("STOP_LOSS", current_premium)
        if current_premium >= tgt:
            return self._exit_trade("TARGET_HIT", current_premium)
        return None

    def _exit_trade(self, reason: str, exit_premium: float) -> dict:
        entry = self.position['entry_prem'] or exit_premium
        pnl   = (exit_premium - entry) * self.position['quantity']
        self.day_pnl    += pnl
        self.current_cap += pnl

        logger.info(f"EXIT [{reason}] | Premium: ₹{exit_premium:.1f} | "
                    f"P&L: ₹{pnl:,.0f} | Day P&L: ₹{self.day_pnl:,.0f}")

        order = {
            "action"    : "SELL",
            "symbol"    : self.position['symbol'],
            "exchange"  : "NFO",
            "quantity"  : self.position['quantity'],
            "order_type": "MARKET",
            "reason"    : reason,
            "pnl"       : round(pnl, 0),
        }

        self.position = None
        self.state    = TradeState.DONE

        # Check daily limits — stop trading for the rest of day
        daily_loss_limit = -self.current_cap * config.DAILY_LOSS_LIMIT  / 100
        daily_target     =  self.current_cap * config.DAILY_TARGET / 100

        if self.day_pnl <= daily_loss_limit:
            logger.warning(f"DAILY LOSS LIMIT HIT (₹{self.day_pnl:,.0f}) — no more trades today")
            self.state = TradeState.DAY_OVER
        elif self.day_pnl >= daily_target:
            logger.info(f"DAILY TARGET HIT ₹{self.day_pnl:,.0f} — stopping for the day. Well done!")
            self.state = TradeState.DAY_OVER

        return order

    # ── Status summary ────────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "state"      : self.state,
            "day_pnl"    : round(self.day_pnl, 0),
            "capital"    : round(self.current_cap, 0),
            "trade_count": self.trade_count,
            "orb_valid"  : self.orb.is_valid(),
            "orb_range"  : round(self.orb.range_size, 0),
            "vwap"       : round(self.vwap.value, 0) if self.vwap.value else None,
            "position"   : self.position,
        }
