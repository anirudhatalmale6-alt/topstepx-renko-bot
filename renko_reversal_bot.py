"""
TopstepX Renko Reversal Bot
============================
Strategy: Renko brick reversal on 30-second price sampling
- Build Renko bricks (0.25 pt brick size) from 30s price samples
- Entry: brick color flips (red->green = LONG, green->red = SHORT)
- Exit: immediate on next brick reversal OR TP hit
- TP: $20 total PnL, SL: fixed 20 pts
- ML (logistic regression) learns to skip bad setups over time

Usage:
    python renko_reversal_bot.py --symbols "NQ:1" --tick-interval 1
"""

import asyncio
import argparse
import gc
import math
import signal
import json
import os
import time
import threading
import random
import urllib.request
import urllib.error
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pytz


# ============================================================
# Telegram / ntfy / TradersPost helpers
# ============================================================

def send_telegram(token: str, chat_id: str, message: str):
    if not token or not chat_id:
        return
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[TG] Send failed: {e}")
            return
        except Exception as e:
            print(f"[TG] Send failed: {e}")
            return


def send_ntfy(topic: str, message: str):
    if not topic:
        return
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=message.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
            urllib.request.urlopen(req, timeout=10)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[NTFY] Send failed: {e}")
            return
        except Exception as e:
            print(f"[NTFY] Send failed: {e}")
            return


def send_traderspost(webhook_url: str, action: str, symbol: str, price: float, qty: int):
    if not webhook_url:
        return
    try:
        payload = {"ticker": symbol, "action": action, "price": price, "quantity": qty}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print(f"[TP-WEBHOOK] Sent {action} {symbol} @ {price:.2f}")
    except Exception as e:
        print(f"[TP-WEBHOOK] Failed: {e}")


def send_signals(token: str, chat_id: str, keys: list, direction: str, symbol: str,
                 price: float, qty: int, ntfy_topic: str = "",
                 tp_webhooks: list = None):
    for i, key in enumerate(keys):
        if i > 0:
            time.sleep(0.5)
        msg = f"SIGNAL|{key}|{direction}|{symbol}|{price}|{qty}"
        send_telegram(token, chat_id, msg)
        send_ntfy(ntfy_topic, msg)
    if tp_webhooks:
        if direction == "FLAT":
            tp_action = "exit"
        elif direction == "LONG":
            tp_action = "buy"
        else:
            tp_action = "sell"
        for url in tp_webhooks:
            send_traderspost(url, tp_action, symbol, price, qty)


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(18, 0, 0)
SESSION_END = dtime(16, 0, 0)
TRADING_DAYS = [0, 1, 2, 3, 4, 6]
BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

TRADE_SESSION_START = dtime(18, 0, 0)
TRADE_SESSION_END = dtime(16, 0, 0)

CANDLE_SECONDS = 30
BB_CANDLE_SECONDS = 15
BRICK_SIZE = 0.25

# Fixed TP/SL (no RL — ML only filters entries)
DEFAULT_TP_PTS = 2
DEFAULT_SL_PTS = 20

# DCA strategy: enter 1 contract, add 1 more at -$80, TP at $20 total
DCA_TP_DOLLARS = 20.0
DCA_ADD_THRESHOLD = -80.0
DCA_MAX_CONTRACTS = 2

# Trailing profit: activates when unrealized >= threshold, trails back by trail_pct
TRAIL_PROFIT_ACTIVATE = 20.0   # activate trailing profit at $20 unrealized
TRAIL_PROFIT_PULLBACK = 0.40   # allow 40% pullback from peak (keeps 60%)

DAILY_LOSS_LIMIT = 1000.0

BB_PERIOD = 20
BB_STD = 2.0

# Estimated round-trip fee per contract (commission + exchange)
FEE_PER_CONTRACT = 4.60

MINTICK_VALUES = {
    "NQ": 0.25, "ES": 0.25, "MNQ": 0.25, "MES": 0.25,
    "YM": 1.0, "RTY": 0.10,
}
POINT_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0,
    "YM": 5.0, "RTY": 10.0,
}

WATCHDOG_TIMEOUT = 300
TICK_HEALTH_TIMEOUT = 90


def in_session() -> bool:
    now = datetime.now(ET)
    if now.weekday() not in TRADING_DAYS:
        return False
    t = now.time()
    if SESSION_START > SESSION_END:
        return t >= SESSION_START or t < SESSION_END
    return SESSION_START <= t < SESSION_END


def in_blackout() -> bool:
    if BLACKOUT_START == BLACKOUT_END:
        return False
    t = datetime.now(ET).time()
    return BLACKOUT_START <= t < BLACKOUT_END


def in_trade_session() -> bool:
    t = datetime.now(ET).time()
    if TRADE_SESSION_START > TRADE_SESSION_END:
        return t >= TRADE_SESSION_START or t < TRADE_SESSION_END
    return TRADE_SESSION_START <= t < TRADE_SESSION_END


# ============================================================
# Renko Brick Builder
# ============================================================

class CandleBuilder:
    """Builds fixed-interval candles from tick data."""

    def __init__(self, interval_secs: int = CANDLE_SECONDS):
        self.interval = interval_secs
        self.candles = []
        self._current = None
        self._max_candles = 500

    def _candle_start(self, ts: float) -> float:
        dt = datetime.fromtimestamp(ts, tz=ET)
        sod = dt.hour * 3600 + dt.minute * 60 + dt.second
        candle_sec = (sod // self.interval) * self.interval
        h, rem = divmod(candle_sec, 3600)
        m, s = divmod(rem, 60)
        return dt.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()

    def feed(self, price: float, ts: float = None) -> dict:
        if ts is None:
            ts = time.time()
        candle_start = self._candle_start(ts)

        if self._current is None:
            self._current = {"start": candle_start, "open": price, "high": price,
                             "low": price, "close": price, "volume": 1}
            return None

        if candle_start > self._current["start"]:
            completed = dict(self._current)
            self.candles.append(completed)
            if len(self.candles) > self._max_candles:
                self.candles = self.candles[-self._max_candles:]
            self._current = {"start": candle_start, "open": price, "high": price,
                             "low": price, "close": price, "volume": 1}
            return completed

        self._current["high"] = max(self._current["high"], price)
        self._current["low"] = min(self._current["low"], price)
        self._current["close"] = price
        self._current["volume"] += 1
        return None


def calc_bb(candles: list, period: int = BB_PERIOD, std_mult: float = BB_STD):
    """Calculate Bollinger Bands from candle closes. Returns (upper, middle, lower) or None."""
    if len(candles) < period:
        return None
    closes = np.array([c["close"] for c in candles[-period:]])
    middle = float(np.mean(closes))
    std = float(np.std(closes))
    return (middle + std_mult * std, middle, middle - std_mult * std)


class RenkoBrickBuilder:
    """Builds Renko bricks from 30s candle closes (matches TradingView)."""

    def __init__(self, brick_size: float = BRICK_SIZE):
        self.brick_size = brick_size
        self.bricks = []
        self._last_close = None
        self._last_direction = None
        self._max_bricks = 500

    def feed(self, price: float) -> list:
        new_bricks = []

        if self._last_close is None:
            self._last_close = round(price / self.brick_size) * self.brick_size
            return new_bricks

        ref = self._last_close

        if self._last_direction == "green" or self._last_direction is None:
            while price >= ref + self.brick_size:
                brick = {
                    "open": ref,
                    "close": ref + self.brick_size,
                    "direction": "green",
                    "time": time.time(),
                }
                new_bricks.append(brick)
                ref = ref + self.brick_size
                self._last_direction = "green"

            if not new_bricks:
                reversal_ref = self._last_close
                while price <= reversal_ref - 2 * self.brick_size:
                    brick = {
                        "open": reversal_ref,
                        "close": reversal_ref - self.brick_size,
                        "direction": "red",
                        "time": time.time(),
                    }
                    new_bricks.append(brick)
                    reversal_ref = reversal_ref - self.brick_size
                    self._last_direction = "red"

        elif self._last_direction == "red":
            while price <= ref - self.brick_size:
                brick = {
                    "open": ref,
                    "close": ref - self.brick_size,
                    "direction": "red",
                    "time": time.time(),
                }
                new_bricks.append(brick)
                ref = ref - self.brick_size
                self._last_direction = "red"

            if not new_bricks:
                reversal_ref = self._last_close
                while price >= reversal_ref + 2 * self.brick_size:
                    brick = {
                        "open": reversal_ref,
                        "close": reversal_ref + self.brick_size,
                        "direction": "green",
                        "time": time.time(),
                    }
                    new_bricks.append(brick)
                    reversal_ref = reversal_ref + self.brick_size
                    self._last_direction = "green"

        if new_bricks:
            self._last_close = new_bricks[-1]["close"]
            self.bricks.extend(new_bricks)
            if len(self.bricks) > self._max_bricks:
                self.bricks = self.bricks[-self._max_bricks:]

        return new_bricks

    def last_brick(self):
        return self.bricks[-1] if self.bricks else None

    def last_direction(self):
        return self._last_direction

    def consecutive_count(self) -> int:
        if not self.bricks:
            return 0
        d = self.bricks[-1]["direction"]
        count = 0
        for b in reversed(self.bricks):
            if b["direction"] == d:
                count += 1
            else:
                break
        return count

    def get_closes(self, n=None):
        closes = [b["close"] for b in self.bricks]
        return closes[-n:] if n else closes


# ============================================================
# Reinforcement Learning for Renko Reversal
# ============================================================

ML_WARMUP_TRADES = 15
ML_LEARNING_RATE = 0.05
ML_SKIP_THRESHOLD = 0.25


class TradeFilter:
    """Online logistic regression — learns to skip bad setups."""

    N_FEATURES = 6

    def __init__(self, data_file: str):
        self.data_file = data_file
        self.weights = [0.0] * self.N_FEATURES
        self.bias = 0.0
        self.trades = []
        self.total_trades = 0
        self.recent_outcomes = []
        self._load()

    @staticmethod
    def _sigmoid(x):
        x = max(-20.0, min(20.0, x))
        return 1.0 / (1.0 + math.exp(-x))

    def _featurize(self, features: dict) -> list:
        consec = min(features.get("consecutive_bricks", 1), 10) / 10.0
        mom = features.get("momentum_pts", 0.0) / 5.0
        hour = (features.get("hour", 12) - 12) / 4.0
        day = (datetime.now(ET).weekday() - 2) / 2.0
        recent = self.recent_outcomes[-10:]
        recent_wr = sum(1 for p in recent if p > 0) / max(len(recent), 1)
        bricks = min(features.get("brick_count", 50), 200) / 200.0
        return [consec, mom, hour, day, recent_wr, bricks]

    def _predict(self, x: list) -> float:
        z = self.bias + sum(w * xi for w, xi in zip(self.weights, x))
        return self._sigmoid(z)

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r") as f:
                    data = json.load(f)
                self.weights = data.get("weights", [0.0] * self.N_FEATURES)
                self.bias = data.get("bias", 0.0)
                self.trades = data.get("trades", [])
                self.total_trades = data.get("total_trades", len(self.trades))
                self.recent_outcomes = data.get("recent_outcomes", [])
                while len(self.weights) < self.N_FEATURES:
                    self.weights.append(0.0)
                print(f"[ML] Loaded: {self.total_trades} trades, "
                      f"bias={self.bias:.3f}")
            except Exception as e:
                print(f"[ML] Load error: {e}")

    def _save(self):
        try:
            data = {
                "weights": self.weights,
                "bias": self.bias,
                "trades": self.trades[-500:],
                "total_trades": self.total_trades,
                "recent_outcomes": self.recent_outcomes[-20:],
            }
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.data_file)
        except Exception as e:
            print(f"[ML] Save error: {e}")

    def extract_features(self, price: float, renko: RenkoBrickBuilder) -> dict:
        hour = datetime.now(ET).hour
        consec = renko.consecutive_count()
        last_dir = renko.last_direction() or "none"
        closes = renko.get_closes(10)
        if len(closes) >= 6:
            mom_pts = (closes[-1] - closes[-6]) / 5.0
        else:
            mom_pts = 0.0
        if mom_pts > 3:
            momentum = "strong_up"
        elif mom_pts > 1:
            momentum = "up"
        elif mom_pts < -3:
            momentum = "strong_down"
        elif mom_pts < -1:
            momentum = "down"
        else:
            momentum = "flat"
        return {
            "consecutive_bricks": consec,
            "last_direction": last_dir,
            "momentum": momentum,
            "momentum_pts": round(mom_pts, 2),
            "hour": hour,
            "price": round(price, 2),
            "brick_count": len(renko.bricks),
        }

    def should_enter(self, features: dict) -> tuple:
        if self.total_trades < ML_WARMUP_TRADES:
            return True, f"ML warmup ({self.total_trades}/{ML_WARMUP_TRADES})"
        x = self._featurize(features)
        p_win = self._predict(x)
        if p_win < ML_SKIP_THRESHOLD:
            return False, f"ML|P(win)={p_win:.2f}<{ML_SKIP_THRESHOLD}|SKIP"
        return True, f"ML|P(win)={p_win:.2f}|ENTER"

    def record_trade(self, features: dict, pnl: float, source: str = "live",
                     entered: bool = True, mae: float = 0.0, mfe: float = 0.0,
                     **kwargs):
        if features is None:
            return
        x = self._featurize(features)
        y = 1.0 if pnl > 0 else 0.0
        p = self._predict(x)
        error = y - p
        for i in range(self.N_FEATURES):
            self.weights[i] += ML_LEARNING_RATE * error * x[i]
        self.bias += ML_LEARNING_RATE * error

        self.recent_outcomes.append(pnl)
        if len(self.recent_outcomes) > 20:
            self.recent_outcomes = self.recent_outcomes[-20:]

        trade = {
            "features": features, "pnl": pnl,
            "win": 1 if pnl > 0 else 0, "entered": entered,
            "p_win": round(p, 3), "source": source,
            "timestamp": datetime.now(ET).isoformat(),
            "mae": mae, "mfe": mfe,
        }
        self.trades.append(trade)
        self.total_trades += 1
        self._save()

        wins = sum(1 for t in self.trades if t["win"] == 1)
        total = len(self.trades)
        print(f"[ML] Trade recorded: PnL=${pnl:.2f} | P(win)={p:.2f} | "
              f"{total} trades, {wins} wins ({100*wins/total:.0f}%)")

    def stats(self) -> str:
        if not self.trades:
            return "No trades recorded"
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"] == 1)
        total_pnl = sum(t["pnl"] for t in self.trades)
        return (f"ML: {total} trades | W:{wins} L:{total-wins} | "
                f"Win%: {100*wins/total:.0f}% | PnL: ${total_pnl:.2f}")


# ============================================================
# Per-Symbol Strategy State
# ============================================================

class SymbolState:
    def __init__(self, symbol: str, base_qty: int, ntfy_topic: str,
                 tg_token: str, tg_chat: str, tg_keys: list,
                 tp_webhooks: list = None):
        self.symbol = symbol
        self.base_qty = base_qty
        self.ntfy_topic = ntfy_topic
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys
        self.tp_webhooks = tp_webhooks or []
        self.pv = POINT_VALUES.get(symbol, 20.0)

        self.renko = RenkoBrickBuilder(BRICK_SIZE)
        self.candles = CandleBuilder(CANDLE_SECONDS)
        self.bb_candles = CandleBuilder(BB_CANDLE_SECONDS)
        self.last_price = 0.0
        self.last_tick_time = 0.0
        self._prev_brick_dir = None
        self._pending_signal = None  # "LONG" or "SHORT" — set by Renko, waiting for BB confirm
        self._tp_limit_order_id = None  # Tracks active TP limit order
        self._tp_limit_price = None
        self._trail_profit_active = False
        self._trail_profit_peak = 0.0    # highest unrealized seen
        self._trail_profit_floor = 0.0   # exit if unrealized drops to this

        self.position = 0
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_features = None
        self.active_sl_pts = DEFAULT_SL_PTS
        self.live_pnl = 0.0
        self.trade_mae = 0.0
        self.trade_mfe = 0.0
        self._dca_done = False
        self._entry_prices = []

        self.ml = TradeFilter(os.path.join(os.getcwd(), f"ml_state_{symbol}.json"))

        self.ctx = None
        self._suite_client = None
        self._pending_rl = None
        self._start_balance = None
        self._pnl_session_day = None

        self.daily_loss = 0.0

    def extract_features(self) -> dict:
        return self.ml.extract_features(price=self.last_price, renko=self.renko)

    async def _sync_entry_price_from_platform(self) -> None:
        """Fetch the real averagePrice from the platform position after entry/DCA."""
        if not self._suite_client or self.position == 0:
            return
        try:
            await asyncio.sleep(0.3)
            positions = await asyncio.wait_for(
                self._suite_client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    old_avg = self.entry_price
                    self.entry_price = p.averagePrice
                    self.contracts_held = p.size
                    if abs(old_avg - p.averagePrice) > 0.01:
                        print(f"[{self.symbol} PRICE-SYNC] Entry corrected: "
                              f"{old_avg:.2f} -> {p.averagePrice:.2f} (platform)")
                    break
        except Exception as e:
            print(f"[{self.symbol}] Entry price sync failed: {e}")

    async def _get_real_unrealized(self) -> float:
        """Query platform balance to get REAL unrealized PnL including fees/slippage."""
        if not self._suite_client or self._start_balance is None:
            return None
        try:
            accounts = await asyncio.wait_for(
                self._suite_client.list_accounts(), timeout=8.0)
            acct_name = os.environ.get("PROJECT_X_ACCOUNT_NAME", "")
            for a in accounts:
                if a.name == acct_name:
                    real_total = a.balance - self._start_balance
                    real_unrealized = real_total - self.live_pnl
                    return real_unrealized
        except Exception as e:
            print(f"[{self.symbol}] Real unrealized check failed: {e}")
        return None

    async def _sync_pnl_from_platform(self) -> None:
        if not self._suite_client or self.position != 0:
            return
        try:
            await asyncio.sleep(0.5)
            accounts = await asyncio.wait_for(
                self._suite_client.list_accounts(), timeout=8.0)
            acct_name = os.environ.get("PROJECT_X_ACCOUNT_NAME", "")
            for a in accounts:
                if a.name == acct_name:
                    if self._start_balance is None:
                        self._start_balance = a.balance
                        self.live_pnl = 0.0
                        print(f"[{self.symbol} PNL-SYNC] Baseline set: ${self._start_balance:,.2f} "
                              f"(live_pnl reset to 0)")
                    else:
                        real_pnl = a.balance - self._start_balance
                        drift = abs(real_pnl - self.live_pnl)
                        if drift > 2.0:
                            print(f"[{self.symbol} PNL-SYNC] Bot: ${self.live_pnl:.2f} -> "
                                  f"Platform: ${real_pnl:.2f} (drift ${drift:.2f})")
                            self.live_pnl = real_pnl
                    break
        except Exception as e:
            print(f"[{self.symbol}] PnL sync failed: {e}")

    async def _ensure_flat_before_entry(self):
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=4.0)
        except Exception:
            pass

    async def _enter_long(self, price: float):
        if self.position != 0:
            return False
        await self._ensure_flat_before_entry()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")

        features = self.extract_features()

        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG x{qty} @ {price:.2f} | "
              f"TP=${DCA_TP_DOLLARS} | SL={DEFAULT_SL_PTS}pts | DCA at -${abs(DCA_ADD_THRESHOLD)} | "
              f"Session P&L: ${self.live_pnl:.2f}")

        try:
            response = await asyncio.wait_for(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=0, size=qty),
                timeout=15.0)
            if response.success:
                self.position = 1
                self.contracts_held = qty
                self.entry_price = price
                self._entry_prices = [price]
                self._dca_done = False
                self.entry_time = time.time()
                self.entry_features = features
                self.active_sl_pts = DEFAULT_SL_PTS
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "LONG", self.symbol, price, qty),
                    kwargs={"ntfy_topic": self.ntfy_topic,
                            "tp_webhooks": self.tp_webhooks}, daemon=True).start()
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"[{self.symbol}] Order FAILED: {response}")
                return False
        except asyncio.TimeoutError:
            print(f"[{self.symbol}] Order TIMEOUT")
            return False
        except Exception as e:
            print(f"[{self.symbol}] Order ERROR: {e}")
            return False

    async def _enter_short(self, price: float):
        if self.position != 0:
            return False
        await self._ensure_flat_before_entry()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")

        features = self.extract_features()

        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT x{qty} @ {price:.2f} | "
              f"TP=${DCA_TP_DOLLARS} | SL={DEFAULT_SL_PTS}pts | DCA at -${abs(DCA_ADD_THRESHOLD)} | "
              f"Session P&L: ${self.live_pnl:.2f}")

        try:
            response = await asyncio.wait_for(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=1, size=qty),
                timeout=15.0)
            if response.success:
                self.position = -1
                self.contracts_held = qty
                self.entry_price = price
                self._entry_prices = [price]
                self._dca_done = False
                self.entry_time = time.time()
                self.entry_features = features
                self.active_sl_pts = DEFAULT_SL_PTS
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "SHORT", self.symbol, price, qty),
                    kwargs={"ntfy_topic": self.ntfy_topic,
                            "tp_webhooks": self.tp_webhooks}, daemon=True).start()
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"[{self.symbol}] Order FAILED: {response}")
                return False
        except asyncio.TimeoutError:
            print(f"[{self.symbol}] Order TIMEOUT")
            return False
        except Exception as e:
            print(f"[{self.symbol}] Order ERROR: {e}")
            return False

    async def _dca_add(self, price: float):
        qty = self.base_qty
        side = 0 if self.position == 1 else 1
        direction = "LONG" if self.position == 1 else "SHORT"
        now = datetime.now(ET).strftime("%H:%M:%S")

        print(f"\n[{now}] [{self.symbol} DCA] Adding {direction} x{qty} @ {price:.2f} | "
              f"Now {self.contracts_held + qty} contracts | Avg: {self.entry_price:.2f} -> ", end="")

        try:
            response = await asyncio.wait_for(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=side, size=qty),
                timeout=15.0)
            if response.success:
                self._entry_prices.append(price)
                old_total = self.entry_price * self.contracts_held
                self.contracts_held += qty
                self.entry_price = (old_total + price * qty) / self.contracts_held
                self._dca_done = True
                print(f"{self.entry_price:.2f}")
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"FAILED: {response}")
                return False
        except Exception as e:
            print(f"ERROR: {e}")
            return False

    async def _flatten(self, price: float, reason: str = "signal"):
        if self.position == 0:
            return
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl_before = self.live_pnl

        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=15.0)
        except Exception as e:
            print(f"[{now_str}] [{self.symbol}] CLOSE ERROR: {e}")
            return

        if self.position == 1:
            pts = price - self.entry_price
        else:
            pts = self.entry_price - price
        trade_pnl = pts * self.pv * self.contracts_held
        self.live_pnl += trade_pnl
        self.daily_loss += min(0, trade_pnl)

        print(f"[{now_str}] [{self.symbol}] <<< EXITING {direction} x{self.contracts_held} "
              f"@ {price:.2f} | Trade: ${trade_pnl:.2f} | Session: ${self.live_pnl:.2f} | "
              f"{reason}")

        self.position = 0
        self.contracts_held = 0
        self._tp_limit_order_id = None
        self._tp_limit_price = None
        self._trail_profit_active = False
        self._trail_profit_peak = 0.0
        self._trail_profit_floor = 0.0

        threading.Thread(target=send_signals, args=(
            self.tg_token, self.tg_chat, self.tg_keys,
            "FLAT", self.symbol, price, 0),
            kwargs={"ntfy_topic": self.ntfy_topic,
                    "tp_webhooks": self.tp_webhooks}, daemon=True).start()

        await self._sync_pnl_from_platform()
        real_trade_pnl = self.live_pnl - pnl_before
        if abs(real_trade_pnl - trade_pnl) > 2.0:
            print(f"[{self.symbol}] PnL correction: estimated ${trade_pnl:.2f} -> "
                  f"actual ${real_trade_pnl:.2f}")
            trade_pnl = real_trade_pnl

        feat = self._pending_rl["features"] if self._pending_rl else self.entry_features
        m = self._pending_rl.get("mae", self.trade_mae) if self._pending_rl else self.trade_mae
        mf = self._pending_rl.get("mfe", self.trade_mfe) if self._pending_rl else self.trade_mfe

        if feat:
            self.ml.record_trade(feat, trade_pnl, source="live",
                                 entered=True, mae=m, mfe=mf)

        self._pending_rl = None
        self.entry_features = None

    def tick(self, price: float, ts: float = None):
        """Process a tick. DCA Renko reversal strategy."""
        if ts is None:
            ts = time.time()
        self.last_price = price
        self.last_tick_time = ts
        actions = []

        now_et = datetime.now(ET)
        session_day = now_et.date() if now_et.time() >= SESSION_START else now_et.date() - timedelta(days=1)
        if self._pnl_session_day is None or self._pnl_session_day != session_day:
            self._pnl_session_day = session_day
            self._start_balance = None
            self.live_pnl = 0.0
            self.daily_loss = 0.0
            print(f"[{self.symbol} SESSION] New session day {session_day} — all reset")

        completed_candle = self.candles.feed(price, ts)
        self.bb_candles.feed(price, ts)
        new_bricks = []
        if completed_candle:
            candle_close = completed_candle["close"]
            new_bricks = self.renko.feed(candle_close)
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{self.symbol} CANDLE] close={candle_close:.2f} "
                  f"H={completed_candle['high']:.2f} L={completed_candle['low']:.2f} "
                  f"vol={completed_candle['volume']}")

        if self.position != 0:
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.pv * self.contracts_held
            else:
                unrealized = (self.entry_price - price) * self.pv * self.contracts_held
            self.trade_mfe = max(self.trade_mfe, unrealized)
            self.trade_mae = min(self.trade_mae, unrealized)

            # TRAILING PROFIT: lock in gains once profit exceeds threshold
            if unrealized >= TRAIL_PROFIT_ACTIVATE:
                if not self._trail_profit_active:
                    self._trail_profit_active = True
                    self._trail_profit_peak = unrealized
                    self._trail_profit_floor = unrealized * (1.0 - TRAIL_PROFIT_PULLBACK)
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    direction = "LONG" if self.position == 1 else "SHORT"
                    print(f"[{now_str}] [{self.symbol} TRAIL-PROFIT] {direction} "
                          f"activated at ${unrealized:.0f} | floor=${self._trail_profit_floor:.0f}")
                elif unrealized > self._trail_profit_peak:
                    self._trail_profit_peak = unrealized
                    self._trail_profit_floor = unrealized * (1.0 - TRAIL_PROFIT_PULLBACK)

            if self._trail_profit_active and unrealized <= self._trail_profit_floor:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.symbol} TRAIL-PROFIT-EXIT] {direction} "
                      f"unrealized ${unrealized:.0f} <= floor ${self._trail_profit_floor:.0f} "
                      f"(peak was ${self._trail_profit_peak:.0f})")
                self._pending_rl = {"features": self.entry_features,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "TRAIL_PROFIT"))
                return actions

            # TP check: place limit order for guaranteed $20 net
            if unrealized >= DCA_TP_DOLLARS and not self._tp_limit_order_id:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.symbol} TP-NEAR] {direction} x{self.contracts_held} "
                      f"est ${unrealized:.0f} >= ${DCA_TP_DOLLARS} @ {price:.2f} — placing TP limit...")
                self._pending_rl = {"features": self.entry_features,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                actions.append(("tp_limit", price))
                return actions

            # DCA: add 1 contract at -$80 (checked BEFORE SL so price gaps don't skip DCA)
            if not self._dca_done and self.contracts_held < DCA_MAX_CONTRACTS:
                if unrealized <= DCA_ADD_THRESHOLD:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} DCA-TRIGGER] unrealized ${unrealized:.0f} "
                          f"<= ${DCA_ADD_THRESHOLD:.0f}")
                    if self._tp_limit_order_id:
                        actions.append(("cancel_tp_limit",))
                    actions.append(("dca_add", price))
                    return actions

            # STOP LOSS: exit if loss exceeds SL (in points)
            sl_dollars = self.active_sl_pts * self.pv * self.contracts_held
            if unrealized <= -sl_dollars:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.symbol} SL] {direction} x{self.contracts_held} "
                      f"unrealized ${unrealized:.0f} <= -${sl_dollars:.0f} "
                      f"(SL={self.active_sl_pts}pts) — STOP LOSS EXIT")
                self._pending_rl = {"features": self.entry_features,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "STOP_LOSS"))
                return actions

            # HARD BACKSTOP: force exit if loss exceeds daily limit (safety net)
            if unrealized <= -DAILY_LOSS_LIMIT:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.symbol} MAX-LOSS] {direction} x{self.contracts_held} "
                      f"unrealized ${unrealized:.0f} <= -${DAILY_LOSS_LIMIT:.0f} — FORCE EXIT")
                self._pending_rl = {"features": self.entry_features,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "MAX_LOSS"))
                return actions

        # Process new bricks
        if new_bricks:
            for brick in new_bricks:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} BRICK] {brick['direction'].upper()} "
                      f"{brick['open']:.2f} -> {brick['close']:.2f} "
                      f"(consecutive: {self.renko.consecutive_count()})")

                # Opposite brick = exit current position ONLY if in profit (or max loss hit)
                if self.position != 0:
                    if self.position == 1:
                        cur_pnl = (price - self.entry_price) * self.pv * self.contracts_held
                    else:
                        cur_pnl = (self.entry_price - price) * self.pv * self.contracts_held

                    should_exit = cur_pnl >= 0 or cur_pnl <= -DAILY_LOSS_LIMIT

                    if self.position == 1 and brick["direction"] == "red":
                        if should_exit:
                            reason_tag = "BRICK-EXIT" if cur_pnl >= 0 else "MAX-LOSS-EXIT"
                            print(f"[{now_str}] [{self.symbol} {reason_tag}] LONG x{self.contracts_held} "
                                  f"closed — red brick (PnL ${cur_pnl:.0f})")
                            self._pending_rl = {"features": self.entry_features,
                                                "mae": self.trade_mae, "mfe": self.trade_mfe}
                            if self._tp_limit_order_id:
                                actions.append(("cancel_tp_limit",))
                            actions.append(("flatten", price, "BRICK_REVERSAL"))
                            self._prev_brick_dir = brick["direction"]
                            return actions
                        else:
                            print(f"[{now_str}] [{self.symbol} BRICK-HOLD] LONG x{self.contracts_held} "
                                  f"— red brick but PnL ${cur_pnl:.0f} < $0, holding")
                    elif self.position == -1 and brick["direction"] == "green":
                        if should_exit:
                            reason_tag = "BRICK-EXIT" if cur_pnl >= 0 else "MAX-LOSS-EXIT"
                            print(f"[{now_str}] [{self.symbol} {reason_tag}] SHORT x{self.contracts_held} "
                                  f"closed — green brick (PnL ${cur_pnl:.0f})")
                            self._pending_rl = {"features": self.entry_features,
                                                "mae": self.trade_mae, "mfe": self.trade_mfe}
                            if self._tp_limit_order_id:
                                actions.append(("cancel_tp_limit",))
                            actions.append(("flatten", price, "BRICK_REVERSAL"))
                            self._prev_brick_dir = brick["direction"]
                            return actions
                        else:
                            print(f"[{now_str}] [{self.symbol} BRICK-HOLD] SHORT x{self.contracts_held} "
                                  f"— green brick but PnL ${cur_pnl:.0f} < $0, holding")

                # Brick flip = set pending signal, wait for 15s BB confirmation
                if self._prev_brick_dir is not None and brick["direction"] != self._prev_brick_dir:
                    if self.position == 0 and in_trade_session():
                        features = self.extract_features()
                        should_enter, reason = self.ml.should_enter(features)
                        print(f"[{now_str}] [{self.symbol} FLIP] {self._prev_brick_dir} -> "
                              f"{brick['direction']} | {reason}")
                        if should_enter:
                            direction = "LONG" if brick["direction"] == "green" else "SHORT"
                            self._pending_signal = direction
                            bb = calc_bb(self.bb_candles.candles)
                            bb_str = f"upper={bb[0]:.2f} lower={bb[2]:.2f}" if bb else "warming up"
                            print(f"[{now_str}] [{self.symbol} PENDING] {direction} — "
                                  f"waiting for 15s BB touch ({bb_str})")

                self._prev_brick_dir = brick["direction"]

        # Check pending signal against 15s BB
        if self._pending_signal and self.position == 0 and in_trade_session():
            if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} DAILY-LIMIT] "
                      f"daily loss ${self.daily_loss:.0f} >= ${DAILY_LOSS_LIMIT:.0f} — no new entries")
                self._pending_signal = None
            else:
                bb = calc_bb(self.bb_candles.candles)
                if bb:
                    upper, middle, lower = bb
                    if self._pending_signal == "SHORT" and price >= upper:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} BB-CONFIRM] SHORT — "
                              f"price {price:.2f} >= upper BB {upper:.2f}")
                        self._pending_signal = None
                        actions.append(("enter_short", price))
                    elif self._pending_signal == "LONG" and price <= lower:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} BB-CONFIRM] LONG — "
                              f"price {price:.2f} <= lower BB {lower:.2f}")
                        self._pending_signal = None
                        actions.append(("enter_long", price))

        return actions

    def save_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "saved_at": time.time(),
            "position": self.position,
            "contracts_held": self.contracts_held,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "entry_features": self.entry_features,
            "active_sl_pts": self.active_sl_pts,
            "live_pnl": self.live_pnl,
            "trade_mae": self.trade_mae,
            "trade_mfe": self.trade_mfe,
            "trail_profit_active": self._trail_profit_active,
            "trail_profit_peak": self._trail_profit_peak,
            "trail_profit_floor": self._trail_profit_floor,
            "daily_loss": self.daily_loss,
            "renko_last_close": self.renko._last_close,
            "renko_last_dir": self.renko._last_direction,
            "renko_bricks": self.renko.bricks[-50:],
            "prev_brick_dir": self._prev_brick_dir,
            "dca_done": self._dca_done,
            "entry_prices": self._entry_prices,
            "candle_current": self.candles._current,
            "candle_history": self.candles.candles[-20:],
            "bb_candle_current": self.bb_candles._current,
            "bb_candle_history": self.bb_candles.candles[-30:],
            "pending_signal": self._pending_signal,
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        age = time.time() - state.get("saved_at", 0)
        self.daily_loss = state.get("daily_loss", 0.0)
        self._prev_brick_dir = state.get("prev_brick_dir")
        self._dca_done = state.get("dca_done", False)
        self._entry_prices = state.get("entry_prices", [])

        renko_bricks = state.get("renko_bricks", [])
        if renko_bricks:
            self.renko.bricks = renko_bricks
            self.renko._last_close = state.get("renko_last_close")
            self.renko._last_direction = state.get("renko_last_dir")

        candle_current = state.get("candle_current")
        if candle_current:
            self.candles._current = candle_current
        candle_history = state.get("candle_history", [])
        if candle_history:
            self.candles.candles = candle_history

        bb_candle_current = state.get("bb_candle_current")
        if bb_candle_current:
            self.bb_candles._current = bb_candle_current
        bb_candle_history = state.get("bb_candle_history", [])
        if bb_candle_history:
            self.bb_candles.candles = bb_candle_history
        self._pending_signal = state.get("pending_signal")

        if age < position_ttl:
            self.position = state.get("position", 0)
            self.contracts_held = state.get("contracts_held", 0)
            self.entry_price = state.get("entry_price", 0.0)
            self.entry_time = state.get("entry_time", 0.0)
            self.entry_features = state.get("entry_features")
            self.active_sl_pts = state.get("active_sl_pts", DEFAULT_SL_PTS)
            self.live_pnl = state.get("live_pnl", 0.0)
            self.trade_mae = state.get("trade_mae", 0.0)
            self.trade_mfe = state.get("trade_mfe", 0.0)
            self._trail_profit_active = state.get("trail_profit_active", False)
            self._trail_profit_peak = state.get("trail_profit_peak", 0.0)
            self._trail_profit_floor = state.get("trail_profit_floor", 0.0)
            print(f"  [{self.symbol}] Restored: pos={self.position}, "
                  f"bricks={len(self.renko.bricks)}, last_dir={self.renko._last_direction}")
            return True
        else:
            print(f"  [{self.symbol}] State too old ({age:.0f}s), cleared")
            return False


# ============================================================
# Main Bot
# ============================================================

class RenkoReversalBot:
    def __init__(self, symbol_configs: list, tg_token: str = "",
                 tg_chat: str = "", tg_keys: list = None,
                 tp_webhooks: list = None):
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tp_webhooks = tp_webhooks or []
        self.running = True
        self.suite = None
        self.states = {}
        self.state_file = os.path.join(os.getcwd(), "bot_state_renko.json")
        self.last_tick_time = time.time()
        self.last_real_tick_time = 0.0
        self._reconnect_count = 0
        self._max_reconnects = 5

        for cfg in symbol_configs:
            sym = cfg["symbol"]
            self.states[sym] = SymbolState(
                symbol=sym, base_qty=cfg["qty"],
                ntfy_topic=cfg.get("ntfy_topic", ""),
                tg_token=tg_token, tg_chat=tg_chat,
                tg_keys=self.tg_keys, tp_webhooks=self.tp_webhooks)

    def _symbols_list(self) -> list:
        return list(self.states.keys())

    def save_all_state(self):
        try:
            state = {sym: st.save_state() for sym, st in self.states.items()}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[STATE] Save error: {e}")

    def load_all_state(self) -> bool:
        if not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            for sym, st in self.states.items():
                if sym in state:
                    st.restore_state(state[sym])
            return True
        except Exception as e:
            print(f"[STATE] Load error: {e}")
            return False

    def _register_websocket_handlers(self):
        conn = self.suite.realtime.user_connection
        _last_pos_event = {}

        def on_logout(*args):
            print("[WS] GatewayLogout received")

        def on_position_event(*args):
            now_ts = time.time()
            try:
                data = args[0] if args else None
                if data is None:
                    return
                if isinstance(data, list):
                    data = data[0] if data else None
                if data is None:
                    return

                contract_id = getattr(data, "contractId", None)
                size = getattr(data, "size", None)
                if contract_id is None or size is None:
                    return
                if size != 0:
                    return

                dedup_key = f"{contract_id}"
                if dedup_key in _last_pos_event and (now_ts - _last_pos_event[dedup_key]) < 2.0:
                    return
                _last_pos_event[dedup_key] = now_ts

                for sym, st in self.states.items():
                    if st.ctx and st.ctx.instrument_info.id == contract_id and st.position != 0:
                        price = st.last_price
                        direction = "LONG" if st.position == 1 else "SHORT"
                        if st.position == 1:
                            pnl_est = (price - st.entry_price) * st.pv * st.contracts_held
                        else:
                            pnl_est = (st.entry_price - price) * st.pv * st.contracts_held

                        features = st.entry_features
                        mae = st.trade_mae
                        mfe = st.trade_mfe

                        st.live_pnl += pnl_est
                        st.position = 0
                        st.contracts_held = 0
                        if st._tp_limit_order_id:
                            tp_info = f" (TP limit filled @ {st._tp_limit_price:.2f})"
                            st._tp_limit_order_id = None
                            st._tp_limit_price = None
                        else:
                            tp_info = ""
                        print(f"[WS] Platform closed {direction} {sym} — est PnL: ${pnl_est:.2f}{tp_info}")

                        threading.Thread(target=send_signals, args=(
                            self.tg_token, self.tg_chat, self.tg_keys,
                            "FLAT", sym, price, 0),
                            kwargs={"ntfy_topic": st.ntfy_topic,
                                    "tp_webhooks": st.tp_webhooks}, daemon=True).start()

                        async def _deferred_ml(st_ref, feat, pnl_before, m, mf):
                            try:
                                await st_ref._sync_pnl_from_platform()
                                real_pnl = st_ref.live_pnl - pnl_before + pnl_est
                                st_ref.ml.record_trade(feat, real_pnl, source="platform_close",
                                                       entered=True, mae=m, mfe=mf)
                            except Exception:
                                st_ref.ml.record_trade(feat, pnl_est, source="platform_close_est",
                                                       entered=True, mae=m, mfe=mf)

                        pnl_before = st.live_pnl - pnl_est
                        try:
                            loop = asyncio.get_event_loop()
                            loop.create_task(_deferred_ml(st, features, pnl_before, mae, mfe))
                        except Exception:
                            st.ml.record_trade(features, pnl_est, source="platform_close_est",
                                               entered=True, mae=mae, mfe=mfe)
                        break
            except Exception as e:
                print(f"[WS] Position event error: {e}")

        conn.on("GatewayLogout", on_logout)
        conn.on("GatewayUserPosition", on_position_event)
        conn.on("PositionUpdate", on_position_event)
        print(f"[BOT] WS handlers registered")

    async def _auto_detect_practice_account(self, symbols):
        configured = os.environ.get("PROJECT_X_ACCOUNT_NAME", "")
        if "PRAC" not in configured.upper():
            return
        try:
            acct = self.suite.client.account_info
            if acct and getattr(acct, 'canTrade', True):
                return
        except Exception:
            pass
        try:
            accounts = await self.suite.client.list_accounts()
            practice = [a for a in accounts if "PRAC" in a.name.upper()
                        and getattr(a, 'canTrade', False) and getattr(a, 'isVisible', True)]
            if not practice:
                print("[AUTO-DETECT] No active practice accounts found")
                return
            new_acct = practice[0]
            if new_acct.name == configured:
                return
            print(f"[AUTO-DETECT] Switching: {configured} -> {new_acct.name}")
            os.environ["PROJECT_X_ACCOUNT_NAME"] = new_acct.name
            send_telegram(self.tg_token, self.tg_chat,
                          f"STATUS|Account switched to {new_acct.name} (liquidation detected)")
            await self.suite.disconnect()
            from project_x_py import TradingSuite
            self.suite = await TradingSuite.create(
                instruments=symbols, timeframes=["1sec"], initial_days=1)
            self._register_websocket_handlers()
            for sym, st in self.states.items():
                st.ctx = self.suite[sym]
                st._suite_client = self.suite.client
        except Exception as e:
            print(f"[AUTO-DETECT] Error: {e}")

    async def run(self):
        from project_x_py import TradingSuite

        symbols = self._symbols_list()
        print(f"[BOT] Renko Reversal Bot starting...")
        print(f"[BOT] Strategy: Renko {BRICK_SIZE}pt bricks, DCA reversal")
        print(f"[BOT] ENTRY: brick color flip -> enter 1 contract")
        print(f"[BOT] DCA: add 1 more at -${abs(DCA_ADD_THRESHOLD)}, max {DCA_MAX_CONTRACTS} contracts")
        print(f"[BOT] EXIT: TP=${DCA_TP_DOLLARS} total PnL / opposite brick color")
        print(f"[BOT] ML: logistic regression skips bad setups, SL={DEFAULT_SL_PTS}pts fixed")
        print(f"[BOT] Session: {TRADE_SESSION_START.strftime('%H:%M')} - "
              f"{TRADE_SESSION_END.strftime('%H:%M')} ET")
        print(f"[BOT] Symbols: {symbols}")

        self.load_all_state()

        try:
            self.suite = await TradingSuite.create(
                instruments=symbols, timeframes=["1sec"], initial_days=1)
        except Exception as e:
            err_msg = str(e)
            configured = os.environ.get("PROJECT_X_ACCOUNT_NAME", "")
            if "not found" in err_msg and "PRAC" in configured.upper():
                import re
                acct_names = re.findall(r'PRAC-V2-[\w-]+', err_msg)
                acct_names = [a for a in set(acct_names) if a != configured]
                if acct_names:
                    new_acct = acct_names[0]
                    print(f"[AUTO-SWITCH] Account gone, switching: {configured} -> {new_acct}")
                    os.environ["PROJECT_X_ACCOUNT_NAME"] = new_acct
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"STATUS|Account auto-switched to {new_acct}")
                    self.suite = await TradingSuite.create(
                        instruments=symbols, timeframes=["1sec"], initial_days=1)
                else:
                    raise
            else:
                raise
        self._register_websocket_handlers()

        print(f"[BOT] Connected to TopstepX")
        acct = os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown")
        print(f"[BOT] Account: {acct}")

        for sym, st in self.states.items():
            st.ctx = self.suite[sym]
            st._suite_client = self.suite.client
            price = await st.ctx.data.get_current_price()
            st.last_price = price if price else 0.0
            print(f"[BOT] {sym} contract: {st.ctx.instrument_info.id}")
            print(f"[BOT] {sym} price: {price:.2f}" if price else f"[BOT] {sym} price: market closed")
            await st._sync_pnl_from_platform()

        await self._auto_detect_practice_account(symbols)

        print(f"[BOT] Session active: {in_session()}")
        print(f"[BOT] Trading LIVE — Renko Reversal strategy ({', '.join(symbols)})")
        print(f"[BOT] Watchdog: {WATCHDOG_TIMEOUT}s timeout")
        print(f"[BOT] Press Ctrl+C to stop")

        for sym, st in self.states.items():
            msg = (f"STATUS|Renko Reversal Bot started\n"
                   f"Account: {acct}\n"
                   f"Brick: {BRICK_SIZE}pt | TP: {DEFAULT_TP_PTS}pts (ML filter)\n"
                   f"Mode: Brick flip entry, reversal stop\n"
                   f"Session P&L: ${st.live_pnl:.2f}")
            threading.Thread(target=send_telegram, args=(
                self.tg_token, self.tg_chat, msg), daemon=True).start()

        # Main tick loop
        self.last_tick_time = time.time()
        self.last_real_tick_time = time.time()
        self._reconnect_count = 0
        self._last_seen_price = {}
        self._was_in_session = False
        save_interval = 15
        last_save = time.time()
        last_status = time.time()
        status_interval = 300
        gc_interval = 120
        last_gc = time.time()
        last_acct_check = time.time()
        acct_check_interval = 60

        while self.running:
            if not in_session():
                await asyncio.sleep(5)
                self.last_tick_time = time.time()
                self._was_in_session = False
                continue

            if in_blackout():
                await asyncio.sleep(1)
                self.last_tick_time = time.time()
                self._was_in_session = False
                continue

            if not self._was_in_session:
                self.last_real_tick_time = time.time()
                self._reconnect_count = 0
                self._last_seen_price = {}
                self._was_in_session = True
                print(f"[SESSION] Entering active session — health monitor starting")

            price_changed = False
            for sym, st in self.states.items():
                try:
                    price = await asyncio.wait_for(
                        st.ctx.data.get_current_price(), timeout=5.0)
                except Exception:
                    continue

                if price is None or price <= 0:
                    continue

                self.last_tick_time = time.time()

                if sym not in self._last_seen_price or price != self._last_seen_price[sym]:
                    self._last_seen_price[sym] = price
                    price_changed = True
                    self.last_real_tick_time = time.time()
                    self._reconnect_count = 0

                actions = st.tick(price, self.last_tick_time)

                for action in actions:
                    if action[0] == "enter_long":
                        await st._enter_long(action[1])
                    elif action[0] == "enter_short":
                        await st._enter_short(action[1])
                    elif action[0] == "dca_add":
                        await st._dca_add(action[1])
                    elif action[0] == "cancel_tp_limit":
                        if st._tp_limit_order_id:
                            try:
                                await asyncio.wait_for(
                                    st.ctx.orders.cancel_order(st._tp_limit_order_id),
                                    timeout=5.0)
                                print(f"[{sym}] TP limit order {st._tp_limit_order_id} cancelled")
                            except Exception as e:
                                print(f"[{sym}] TP limit cancel failed: {e}")
                            st._tp_limit_order_id = None
                            st._tp_limit_price = None
                    elif action[0] == "tp_limit":
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        try:
                            contract_id = st.ctx.instrument_info.id
                            avg_price = st.entry_price
                            size = st.contracts_held
                            if avg_price and size > 0:
                                fees = FEE_PER_CONTRACT * size
                                required_raw = DCA_TP_DOLLARS + fees
                                tick_size = MINTICK_VALUES.get(sym, 0.25)
                                if st.position == 1:
                                    limit_price = avg_price + required_raw / (st.pv * size)
                                    limit_price = math.ceil(limit_price / tick_size) * tick_size
                                    side = 1
                                else:
                                    limit_price = avg_price - required_raw / (st.pv * size)
                                    limit_price = math.floor(limit_price / tick_size) * tick_size
                                    side = 0
                                response = await asyncio.wait_for(
                                    st.ctx.orders.place_limit_order(
                                        contract_id=contract_id, side=side,
                                        size=size, limit_price=limit_price),
                                    timeout=15.0)
                                if response.success:
                                    st._tp_limit_order_id = response.orderId
                                    st._tp_limit_price = limit_price
                                    dir_str = "LONG" if st.position == 1 else "SHORT"
                                    print(f"[{now_str}] [{sym} TP-LIMIT] {dir_str} x{size} "
                                          f"limit @ {limit_price:.2f} (avg entry: {avg_price:.2f}, "
                                          f"fees: ${fees:.2f}, target net: ${DCA_TP_DOLLARS})")
                                else:
                                    print(f"[{now_str}] [{sym} TP-LIMIT] Order failed: {response}")
                            else:
                                print(f"[{now_str}] [{sym} TP-LIMIT] No position tracked locally")
                        except asyncio.TimeoutError:
                            print(f"[{now_str}] [{sym} TP-LIMIT] Timeout placing limit order")
                        except Exception as e:
                            print(f"[{now_str}] [{sym} TP-LIMIT] Error: {type(e).__name__}: {e}")
                    elif action[0] == "flatten":
                        reason = action[2] if len(action) > 2 else "signal"
                        await st._flatten(action[1], reason)

            now = time.time()
            tick_gap = now - self.last_real_tick_time

            if not price_changed and tick_gap > TICK_HEALTH_TIMEOUT:
                self._reconnect_count += 1
                if self._reconnect_count > self._max_reconnects:
                    print(f"[HEALTH] {self._reconnect_count} reconnect attempts failed — restarting process")
                    self.save_all_state()
                    import sys
                    sys.exit(1)
                stale_prices = ", ".join(f"{s}={p:.2f}" for s, p in self._last_seen_price.items())
                print(f"[HEALTH] Price unchanged for {tick_gap:.0f}s (stale: {stale_prices}) — reconnecting "
                      f"(attempt {self._reconnect_count}/{self._max_reconnects})")
                threading.Thread(target=send_telegram, args=(
                    self.tg_token, self.tg_chat,
                    f"STATUS|Stale price for {tick_gap:.0f}s — reconnecting (attempt {self._reconnect_count})"),
                    daemon=True).start()
                try:
                    await self.suite.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(5)
                try:
                    from project_x_py import TradingSuite
                    self.suite = await TradingSuite.create(
                        instruments=symbols, timeframes=["1sec"], initial_days=1)
                    self._register_websocket_handlers()
                    for sym2, st2 in self.states.items():
                        st2.ctx = self.suite[sym2]
                        st2._suite_client = self.suite.client
                    self._last_seen_price = {}
                    self.last_real_tick_time = time.time()
                    self.last_tick_time = time.time()
                    print(f"[HEALTH] Reconnected successfully")
                    threading.Thread(target=send_telegram, args=(
                        self.tg_token, self.tg_chat,
                        f"STATUS|Reconnected successfully after {tick_gap:.0f}s gap"),
                        daemon=True).start()
                except Exception as e:
                    print(f"[HEALTH] Reconnect failed: {e}")
                    await asyncio.sleep(10)
                continue

            if now - last_save > save_interval:
                self.save_all_state()
                last_save = now

            if now - last_status > status_interval:
                for sym, st in self.states.items():
                    brick_str = (f"Last brick: {st.renko.last_direction() or 'none'} "
                                 f"@ {st.renko._last_close:.2f}") if st.renko._last_close else "No bricks"
                    pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[st.position]
                    print(f"  [{sym} @ {datetime.now(ET).strftime('%H:%M:%S')}]")
                    print(f"    Price: {st.last_price:.2f} | {brick_str}")
                    print(f"    Position: {pos_str} x{st.contracts_held} | "
                          f"P&L: ${st.live_pnl:.2f}")
                    print(f"    Bricks: {len(st.renko.bricks)} | {st.ml.stats()}")
                last_status = now

            if now - last_gc > gc_interval:
                gc.collect()
                import resource
                rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] [MEM] RSS: {rss_mb:.0f}MB (gc collected)")
                last_gc = now

            if now - last_acct_check > acct_check_interval:
                last_acct_check = now
                configured = os.environ.get("PROJECT_X_ACCOUNT_NAME", "")
                if "PRAC" in configured.upper():
                    try:
                        accounts = await asyncio.wait_for(
                            self.suite.client.list_accounts(), timeout=10.0)
                        acct_names = [a.name for a in accounts]
                        if configured not in acct_names:
                            prac = [n for n in acct_names if "PRAC" in n.upper()]
                            print(f"[ACCT-CHECK] {configured} gone! Available: {acct_names}")
                            if prac:
                                print(f"[ACCT-CHECK] New practice found: {prac[0]} — restarting to switch")
                            else:
                                print(f"[ACCT-CHECK] No practice accounts — restarting to retry")
                            self.save_all_state()
                            os._exit(1)
                    except Exception:
                        pass

            if time.time() - self.last_tick_time > WATCHDOG_TIMEOUT:
                print(f"[WATCHDOG] No ticks for {time.time() - self.last_tick_time:.0f}s — killing")
                self.save_all_state()
                import sys
                sys.exit(1)

            await asyncio.sleep(1)


# ============================================================
# Entry Point
# ============================================================

def parse_symbol_configs(symbols_str: str) -> list:
    configs = []
    for part in symbols_str.split(","):
        parts = part.strip().split(":")
        if not parts[0]:
            continue
        cfg = {
            "symbol": parts[0].strip().upper(),
            "qty": int(parts[1]) if len(parts) > 1 else 1,
            "ntfy_topic": parts[2].strip() if len(parts) > 2 else "",
        }
        configs.append(cfg)
    return configs


def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko Reversal Bot")
    parser.add_argument("--symbols", default="NQ:1",
                        help="Multi-symbol: 'NQ:1:ntfy-topic,ES:1'")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--tick-interval", type=int, default=1)
    parser.add_argument("--tp-webhooks", default="", help="Comma-separated TradersPost URLs")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []
    tp_webhooks = [u.strip() for u in args.tp_webhooks.split(",") if u.strip()] if args.tp_webhooks else []
    symbol_configs = parse_symbol_configs(args.symbols)

    if not symbol_configs:
        print("ERROR: No symbols configured")
        return

    print(f"[BOT] Renko Reversal Bot")
    print(f"[BOT] Brick: {BRICK_SIZE}pt | TP: {DEFAULT_TP_PTS}pts (ML filter)")
    print(f"[BOT] ENTRY: brick color flip -> enter in new direction")
    print(f"[BOT] EXIT: brick reversal (immediate) / TP hit")
    print(f"[BOT] Session: {TRADE_SESSION_START.strftime('%H:%M')} - "
          f"{TRADE_SESSION_END.strftime('%H:%M')} ET")
    for cfg in symbol_configs:
        print(f"  {cfg['symbol']}: qty={cfg['qty']}")

    stopped = False
    current_bot = None

    def handle_signal(sig, frame):
        nonlocal stopped, current_bot
        stopped = True
        if current_bot:
            current_bot.running = False
            current_bot.save_all_state()
        print("\n[BOT] Shutting down...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def thread_watchdog():
        while not stopped:
            time.sleep(30)
            if not current_bot:
                continue
            real_gap = time.time() - current_bot.last_real_tick_time if current_bot.last_real_tick_time > 0 else 0
            loop_gap = time.time() - current_bot.last_tick_time if current_bot.last_tick_time > 0 else 0
            if in_session() and not in_blackout():
                if real_gap > WATCHDOG_TIMEOUT:
                    print(f"[THREAD-WATCHDOG] No real ticks for {real_gap:.0f}s (loop gap: {loop_gap:.0f}s) — force-killing")
                    current_bot.save_all_state()
                    os._exit(1)
            elif loop_gap > WATCHDOG_TIMEOUT + 120:
                print(f"[THREAD-WATCHDOG] Event loop appears dead (gap {loop_gap:.0f}s) — force-killing")
                current_bot.save_all_state()
                os._exit(1)

    wd = threading.Thread(target=thread_watchdog, daemon=True)
    wd.start()

    retry_delay = 30
    while not stopped:
        bot = RenkoReversalBot(
            symbol_configs=symbol_configs,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            tp_webhooks=tp_webhooks,
        )
        current_bot = bot

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(bot.run())
            retry_delay = 30
        except KeyboardInterrupt:
            stopped = True
        except BaseException as e:
            print(f"[CRASH] {type(e).__name__}: {e}")
            print(f"[CRASH] Restarting in {retry_delay}s...")
            send_telegram(args.tg_token, args.tg_chat,
                          f"STATUS|Renko Reversal bot crashed, restarting in {retry_delay}s")
            retry_delay = min(retry_delay * 2, 300)
        finally:
            bot.save_all_state()
            loop.close()

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
