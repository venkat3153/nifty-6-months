#!/usr/bin/env python3
# =============================================================================
#  MAIN RUNNER — Nifty 50 ORB Options Algo
#
#  How to run:
#  ─────────────────────────────────────────────────────────────────────────────
#  PAPER TRADE (test first, no real money):
#    python main.py
#
#  LIVE TRADE (Step 1 — do this once every morning before 9:15 AM):
#    python broker_kite.py --login
#
#  LIVE TRADE (Step 2 — run the algo):
#    python main.py --live
#
#  ─────────────────────────────────────────────────────────────────────────────
#  What this algo does every day:
#  1. Waits for market open (9:15 AM)
#  2. Captures Opening Range for 15 minutes (9:15–9:30)
#  3. Looks for a breakout above ORB High (Buy Call) or below ORB Low (Buy Put)
#  4. Applies VWAP filter to reject low-quality signals
#  5. Enters 1 lot, with SL at -40% and Target at +80% of entry premium
#  6. Automatically exits at SL, Target, or 3:10 PM
#  7. Stops for the day if daily target (+2%) or loss limit (-1.5%) is hit
#  8. Logs every trade to trade_log.csv
# =============================================================================

import sys
import time
import logging
import csv
import os
from datetime import datetime, date, time as dtime

import config
from strategy import NiftyORBStrategy, TradeState
from broker import get_broker

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt = "%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("algo.log"),
    ]
)
logger = logging.getLogger(__name__)


# ── Trade Logger ──────────────────────────────────────────────────────────────

class TradeLogger:
    """Appends every completed trade to a CSV file."""

    FIELDS = ["date", "entry_time", "symbol", "direction", "lots",
              "entry_prem", "exit_prem", "sl_prem", "tgt_prem",
              "reason", "pnl", "day_pnl", "capital"]

    def __init__(self, filepath: str = config.LOG_FILE):
        self.filepath = filepath
        if not os.path.exists(filepath):
            with open(filepath, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def log(self, trade: dict):
        with open(self.filepath, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore")
            writer.writerow(trade)
        logger.info(f"Trade logged to {self.filepath}")


# ── Live Feed: 1-minute polling loop ─────────────────────────────────────────

def run_live(broker, strategy: NiftyORBStrategy, trade_logger: TradeLogger):
    """
    Main loop — polls broker every 5 seconds, processes 1-min candles.
    """
    logger.info("Starting live feed loop...")
    last_candle_minute = None
    pending_order      = None   # order waiting for fill

    while True:
        now = datetime.now()

        # ── Market hours gate ──────────────────────────────────────────────────
        if now.time() < dtime(9, 15):
            logger.info(f"Waiting for market open... ({now.strftime('%H:%M:%S')})")
            time.sleep(30)
            continue

        if now.time() > dtime(15, 30):
            logger.info("Market closed. Shutting down.")
            break

        # ── Check if a new 1-min candle has closed ────────────────────────────
        current_minute = now.replace(second=0, microsecond=0)
        if current_minute == last_candle_minute:
            time.sleep(5)
            continue
        last_candle_minute = current_minute

        # ── Fetch latest Nifty candle ─────────────────────────────────────────
        try:
            candles = broker.get_nifty_candles(
                from_dt = now.replace(hour=9, minute=15, second=0, microsecond=0),
                to_dt   = now,
            )
            if not candles:
                time.sleep(5)
                continue
            tick = candles[-1]   # latest closed candle
        except Exception as e:
            logger.error(f"Failed to fetch candles: {e}")
            time.sleep(10)
            continue

        # ── Fetch option LTP when in a trade (for SL/target checks) ─────────
        option_ltp = None
        if strategy.state == TradeState.IN_TRADE and strategy.position:
            try:
                option_ltp = broker.get_ltp(
                    strategy.position["symbol"],
                    strategy.position.get("exchange", "NFO"),
                )
            except Exception as e:
                logger.warning(f"Could not fetch option LTP: {e}")

        # ── Feed tick to strategy ─────────────────────────────────────────────
        try:
            order = strategy.on_tick(tick, option_ltp=option_ltp)
        except Exception as e:
            logger.error(f"Strategy error: {e}", exc_info=True)
            continue

        # ── Execute order if strategy emits one ───────────────────────────────
        if order:
            try:
                order_id = broker.place_order(order)

                if order["action"] == "BUY":
                    # Get fill price after a short delay
                    time.sleep(2)
                    try:
                        fill_price = broker.get_ltp(order["symbol"])
                    except Exception:
                        fill_price = tick["close"] * config.PREMIUM_PCT if hasattr(config, "PREMIUM_PCT") else 100.0
                    strategy.set_entry_premium(fill_price)
                    logger.info(f"BUY filled at ₹{fill_price:.1f}")

                elif order["action"] == "SELL":
                    # Log completed trade
                    pos = strategy.position or {}
                    trade_logger.log({
                        "date"      : date.today().isoformat(),
                        "entry_time": pos.get("entry_time", now).strftime("%H:%M"),
                        "symbol"    : order["symbol"],
                        "direction" : pos.get("option_type", "?"),
                        "lots"      : pos.get("lots", 1),
                        "entry_prem": pos.get("entry_prem", "?"),
                        "exit_prem" : order.get("pnl", 0) / (pos.get("quantity", 25)) + (pos.get("entry_prem") or 0),
                        "sl_prem"   : pos.get("sl_prem", "?"),
                        "tgt_prem"  : pos.get("tgt_prem", "?"),
                        "reason"    : order.get("reason", "?"),
                        "pnl"       : order.get("pnl", 0),
                        "day_pnl"   : strategy.day_pnl,
                        "capital"   : strategy.current_cap,
                    })

            except Exception as e:
                logger.error(f"Order execution failed: {e}", exc_info=True)

        # ── Status every 5 minutes ────────────────────────────────────────────
        if now.minute % 5 == 0 and now.second < 10:
            s = strategy.status()
            logger.info(
                f"STATUS | State: {s['state']} | Day P&L: ₹{s['day_pnl']:,} | "
                f"Capital: ₹{s['capital']:,} | ORB: {s['orb_range']} pts | "
                f"VWAP: {s['vwap']}"
            )

        # ── Day over — quit loop ───────────────────────────────────────────────
        if strategy.state == TradeState.DAY_OVER:
            logger.info(f"Day P&L: ₹{strategy.day_pnl:,.0f} | Capital: ₹{strategy.current_cap:,.0f}")
            break

        time.sleep(5)


# ── Paper Trade Simulation (backtesting on today's expected data) ─────────────

def run_paper_simulation(strategy: NiftyORBStrategy, trade_logger: TradeLogger):
    """
    Runs a simulated session using synthetic 1-min ticks.
    Useful to test the strategy logic before going live.
    """
    import numpy as np
    logger.info("Running PAPER TRADE simulation...")

    # Generate realistic synthetic Nifty data for one day
    np.random.seed(int(datetime.now().strftime("%Y%m%d")))
    spot_start = 24000
    minutes    = 375  # 9:15 to 15:30
    returns    = np.random.normal(0.0001, 0.0015, minutes)
    prices     = [spot_start]
    for r in returns:
        prices.append(prices[-1] * (1 + r))

    base_date = datetime.today().replace(hour=9, minute=15, second=0, microsecond=0)

    for i in range(1, minutes):
        candle_time = base_date.replace(minute=(base_date.minute + i) % 60,
                                         hour=base_date.hour + (base_date.minute + i) // 60)
        tick = {
            "time"  : candle_time,
            "open"  : prices[i - 1],
            "high"  : max(prices[i - 1], prices[i]) * 1.001,
            "low"   : min(prices[i - 1], prices[i]) * 0.999,
            "close" : prices[i],
            "volume": float(np.random.randint(50000, 200000)),
        }

        # Simulate option LTP when in trade (ATM premium scales with spot move)
        sim_option_ltp = None
        if strategy.state == TradeState.IN_TRADE and strategy.position:
            pos = strategy.position
            if pos.get("entry_prem") and pos.get("entry_spot"):
                spot_move = (prices[i] - pos["entry_spot"]) / pos["entry_spot"]
                direction = 1 if pos["option_type"] == "CE" else -1
                # Delta ~0.5 for ATM; use 0.5 × spot_move as option move approximation
                opt_change = direction * spot_move * 0.5 * (pos["entry_spot"] / pos["entry_prem"])
                sim_option_ltp = max(0.1, round(pos["entry_prem"] * (1 + opt_change), 1))

        order = strategy.on_tick(tick, option_ltp=sim_option_ltp)

        if order:
            if order["action"] == "BUY":
                # Simulate ATM premium ~0.8% of spot
                fill_price = round(prices[i] * 0.008, 1)
                strategy.set_entry_premium(fill_price)
                # Record entry spot for option LTP simulation
                strategy.position["entry_spot"] = prices[i]
                logger.info(f"[SIM] BUY {order['symbol']} @ ₹{fill_price}")

            elif order["action"] == "SELL":
                logger.info(f"[SIM] SELL {order['symbol']} | Reason: {order.get('reason')} | "
                            f"P&L: ₹{order.get('pnl', 0):,.0f}")
                trade_logger.log({
                    "date"      : date.today().isoformat(),
                    "entry_time": candle_time.strftime("%H:%M"),
                    "symbol"    : order["symbol"],
                    "direction" : "SIM",
                    "lots"      : 1,
                    "entry_prem": "sim",
                    "exit_prem" : "sim",
                    "sl_prem"   : "sim",
                    "tgt_prem"  : "sim",
                    "reason"    : order.get("reason", "?"),
                    "pnl"       : order.get("pnl", 0),
                    "day_pnl"   : strategy.day_pnl,
                    "capital"   : strategy.current_cap,
                })

        if strategy.state == TradeState.DAY_OVER:
            break

    logger.info(f"\nSimulation complete.")
    s = strategy.status()
    logger.info(f"Day P&L:  ₹{s['day_pnl']:,.0f}")
    logger.info(f"Capital:  ₹{s['capital']:,.0f}")
    logger.info(f"Trades:   {s['trade_count']}")


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    live_mode = "--live" in sys.argv

    # Safety guard — warn if switching to live
    if live_mode:
        config.PAPER_TRADE = False
        logger.warning("=" * 60)
        logger.warning("  LIVE TRADING MODE — REAL MONEY AT RISK")
        logger.warning(f"  Capital: ₹{config.CAPITAL:,.0f}")
        logger.warning(f"  Max daily loss: ₹{config.CAPITAL * config.DAILY_LOSS_LIMIT / 100:,.0f}")
        logger.warning(f"  Daily target:   ₹{config.CAPITAL * config.DAILY_TARGET / 100:,.0f}")
        logger.warning("=" * 60)
        confirm = input("Type 'YES' to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            sys.exit(0)

    broker       = get_broker()
    strategy     = NiftyORBStrategy()
    trade_logger = TradeLogger()

    logger.info("=" * 60)
    logger.info("  NIFTY ORB OPTIONS ALGO STARTED")
    logger.info(f"  Capital:      ₹{config.CAPITAL:,.0f}")
    logger.info(f"  Daily Target: ₹{config.CAPITAL * config.DAILY_TARGET / 100:,.0f} (+{config.DAILY_TARGET}%)")
    logger.info(f"  Loss Limit:   ₹{config.CAPITAL * config.DAILY_LOSS_LIMIT / 100:,.0f} (-{config.DAILY_LOSS_LIMIT}%)")
    logger.info(f"  Mode:         {'LIVE' if live_mode else 'PAPER'}")
    logger.info("=" * 60)

    if live_mode:
        run_live(broker, strategy, trade_logger)
    else:
        run_paper_simulation(strategy, trade_logger)


if __name__ == "__main__":
    main()
