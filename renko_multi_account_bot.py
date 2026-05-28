"""
TopstepX Renko MFI + MSS Multi-Account Bot
============================================================
Single process trades all accounts simultaneously.
One signal engine (Renko/MFI/MSS) -> broadcast to all accounts at once.
No shared files, no peer sync, no primary/follow.

Usage:
    python renko_multi_account_bot.py --config accounts.json --symbols "NQ:1"
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
            return
        except Exception:
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
            return
        except Exception:
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
    except Exception:
        pass


def send_signals(token, chat_id, keys, direction, symbol, price, qty,
                 ntfy_topic="", tp_webhooks=None):
    for i, key in enumerate(keys):
        if i > 0:
            time.sleep(0.5)
        msg = f"SIGNAL|{key}|{direction}|{symbol}|{price}|{qty}"
        send_telegram(token, chat_id, msg)
        send_ntfy(ntfy_topic, msg)
    if tp_webhooks:
        tp_action = "exit" if direction == "FLAT" else ("buy" if direction == "LONG" else "sell")
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
MSS_CANDLE_SECONDS = 15
BRICK_SIZE = 1.0

MFI_PERIOD = 14
MFI_OVERSOLD = 20.0
MFI_OVERBOUGHT = 85.0

DEFAULT_TP_PTS = 2
DEFAULT_SL_PTS = 20

DCA_TP_DOLLARS = 100.0
DCA_ADD_THRESHOLD = -220.0
DCA_MAX_CONTRACTS = 2

TRAIL_PROFIT_ACTIVATE = 60.0
TRAIL_PROFIT_PULLBACK = 0.40

DAILY_LOSS_LIMIT = 1000.0
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

RECONNECT_COOLDOWN_BASE = 5
RECONNECT_COOLDOWN_MAX = 120
RECONNECT_COOLDOWN_OK = 30
FROZEN_FEED_THRESHOLD = 180
STALE_DATA_THRESHOLD = 600
HEARTBEAT_INTERVAL = 1800
POSITION_POLL_INTERVAL = 30
PLATFORM_FLAT_THRESHOLD = 3
MAX_BRICKS_PER_FEED = 10000

MSS_SWING_LOOKBACK = 5
MSS_MIN_SWING_PTS = 2.0
MSS_WARMUP_BRICKS = 15

BB_LENGTH = 20
BB_MULT = 2.0


def in_session():
    now = datetime.now(ET)
    if now.weekday() not in TRADING_DAYS:
        return False
    t = now.time()
    if SESSION_START > SESSION_END:
        return t >= SESSION_START or t < SESSION_END
    return SESSION_START <= t < SESSION_END


def in_blackout():
    if BLACKOUT_START == BLACKOUT_END:
        return False
    t = datetime.now(ET).time()
    return BLACKOUT_START <= t < BLACKOUT_END


def in_trade_session():
    t = datetime.now(ET).time()
    if TRADE_SESSION_START > TRADE_SESSION_END:
        return t >= TRADE_SESSION_START or t < TRADE_SESSION_END
    return TRADE_SESSION_START <= t < TRADE_SESSION_END


# ============================================================
# Renko / MSS / Candle Builders (unchanged)
# ============================================================

class CandleBuilder:
    def __init__(self, interval_secs=CANDLE_SECONDS):
        self.interval = interval_secs
        self.candles = []
        self._current = None
        self._max_candles = 500

    def _candle_start(self, ts):
        dt = datetime.fromtimestamp(ts, tz=ET)
        sod = dt.hour * 3600 + dt.minute * 60 + dt.second
        candle_sec = (sod // self.interval) * self.interval
        h, rem = divmod(candle_sec, 3600)
        m, s = divmod(rem, 60)
        return dt.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()

    def feed(self, price, ts=None):
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


class MSSDetector:
    def __init__(self):
        self.swing_highs = []
        self.swing_lows = []
        self._bearish_triggered = False
        self._bullish_triggered = False

    def update_swings(self, bricks):
        if len(bricks) < MSS_WARMUP_BRICKS:
            return
        highs, lows = [], []
        prev_dir = None
        for i, brick in enumerate(bricks):
            curr_dir = brick.get("direction")
            if curr_dir and prev_dir and curr_dir != prev_dir:
                if prev_dir == "green" and curr_dir == "red":
                    highs.append(max(bricks[i - 1]["open"], bricks[i - 1]["close"]))
                elif prev_dir == "red" and curr_dir == "green":
                    lows.append(min(bricks[i - 1]["open"], bricks[i - 1]["close"]))
            prev_dir = curr_dir
        self.swing_highs = highs[-MSS_SWING_LOOKBACK:]
        self.swing_lows = lows[-MSS_SWING_LOOKBACK:]

    def check(self, price):
        if len(self.swing_lows) >= 2 and self.swing_lows[-1] > self.swing_lows[-2]:
            swing_dist = self.swing_lows[-1] - self.swing_lows[-2]
            if swing_dist >= MSS_MIN_SWING_PTS and price < self.swing_lows[-1] and not self._bearish_triggered:
                self._bearish_triggered = True
                return "bearish"
        else:
            self._bearish_triggered = False
        if len(self.swing_highs) >= 2 and self.swing_highs[-1] < self.swing_highs[-2]:
            swing_dist = self.swing_highs[-2] - self.swing_highs[-1]
            if swing_dist >= MSS_MIN_SWING_PTS and price > self.swing_highs[-1] and not self._bullish_triggered:
                self._bullish_triggered = True
                return "bullish"
        else:
            self._bullish_triggered = False
        return None

    def reset(self):
        self._bearish_triggered = False
        self._bullish_triggered = False

    def status(self):
        sh = f"SH={[round(h, 2) for h in self.swing_highs[-3:]]}" if self.swing_highs else "SH=[]"
        sl = f"SL={[round(l, 2) for l in self.swing_lows[-3:]]}" if self.swing_lows else "SL=[]"
        return f"{sh} {sl}"


class RenkoBrickBuilder:
    def __init__(self, brick_size=BRICK_SIZE):
        self.brick_size = brick_size
        self.bricks = []
        self._last_close = None
        self._last_direction = None
        self._max_bricks = 500

    def feed(self, price):
        new_bricks = []
        if self._last_close is None:
            self._last_close = round(price / self.brick_size) * self.brick_size
            return new_bricks
        ref = self._last_close
        iters = 0
        if self._last_direction == "green" or self._last_direction is None:
            while price >= ref + self.brick_size and iters < MAX_BRICKS_PER_FEED:
                iters += 1
                new_bricks.append({"open": ref, "close": ref + self.brick_size,
                                   "direction": "green", "time": time.time()})
                ref += self.brick_size
                self._last_direction = "green"
            if not new_bricks:
                reversal_ref = self._last_close
                while price <= reversal_ref - 2 * self.brick_size and iters < MAX_BRICKS_PER_FEED:
                    iters += 1
                    new_bricks.append({"open": reversal_ref, "close": reversal_ref - self.brick_size,
                                       "direction": "red", "time": time.time()})
                    reversal_ref -= self.brick_size
                    self._last_direction = "red"
        elif self._last_direction == "red":
            while price <= ref - self.brick_size and iters < MAX_BRICKS_PER_FEED:
                iters += 1
                new_bricks.append({"open": ref, "close": ref - self.brick_size,
                                   "direction": "red", "time": time.time()})
                ref -= self.brick_size
                self._last_direction = "red"
            if not new_bricks:
                reversal_ref = self._last_close
                while price >= reversal_ref + 2 * self.brick_size and iters < MAX_BRICKS_PER_FEED:
                    iters += 1
                    new_bricks.append({"open": reversal_ref, "close": reversal_ref + self.brick_size,
                                       "direction": "green", "time": time.time()})
                    reversal_ref += self.brick_size
                    self._last_direction = "green"
        if iters >= MAX_BRICKS_PER_FEED:
            print(f"[RENKO] WARNING: hit safety cap {MAX_BRICKS_PER_FEED} bricks — price={price}")
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

    def consecutive_count(self):
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
# ML Trade Filter
# ============================================================

ML_WARMUP_TRADES = 15
ML_LEARNING_RATE = 0.05
ML_SKIP_THRESHOLD = 0.25


class TradeFilter:
    N_FEATURES = 9

    def __init__(self, data_file):
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

    def _featurize(self, features):
        consec = min(features.get("consecutive_bricks", 1), 10) / 10.0
        mom = features.get("momentum_pts", 0.0) / 5.0
        hour = features.get("hour", 12)
        minute = features.get("minute", 0)
        hour_frac = (hour + minute / 60.0) / 24.0
        hour_sin = math.sin(2 * math.pi * hour_frac)
        hour_cos = math.cos(2 * math.pi * hour_frac)
        weekday = datetime.now(ET).weekday()
        day_sin = math.sin(2 * math.pi * weekday / 7.0)
        day_cos = math.cos(2 * math.pi * weekday / 7.0)
        recent = self.recent_outcomes[-10:]
        recent_wr = sum(1 for p in recent if p > 0) / max(len(recent), 1)
        bricks = min(features.get("brick_count", 50), 200) / 200.0
        if 18 <= hour or hour < 4:
            session = 0.0
        elif hour < 10:
            session = 0.15
        elif hour < 12:
            session = 0.35
        elif hour < 14:
            session = 0.65
        elif hour < 16:
            session = 0.85
        else:
            session = 1.0
        return [consec, mom, hour_sin, hour_cos, day_sin, day_cos, recent_wr, bricks, session]

    def _predict(self, x):
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
                print(f"[ML] Loaded: {self.total_trades} trades, bias={self.bias:.3f}")
            except Exception as e:
                print(f"[ML] Load error: {e}")

    def _save(self):
        try:
            data = {"weights": self.weights, "bias": self.bias,
                    "trades": self.trades[-500:], "total_trades": self.total_trades,
                    "recent_outcomes": self.recent_outcomes[-20:]}
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.data_file)
        except Exception:
            pass

    def extract_features(self, price, renko):
        now_et = datetime.now(ET)
        consec = renko.consecutive_count()
        closes = renko.get_closes(10)
        mom_pts = (closes[-1] - closes[-6]) / 5.0 if len(closes) >= 6 else 0.0
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
            "last_direction": renko.last_direction() or "none",
            "momentum": momentum, "momentum_pts": round(mom_pts, 2),
            "hour": now_et.hour, "minute": now_et.minute,
            "price": round(price, 2), "brick_count": len(renko.bricks),
        }

    def should_enter(self, features):
        if self.total_trades < ML_WARMUP_TRADES:
            return True, f"ML warmup ({self.total_trades}/{ML_WARMUP_TRADES})"
        x = self._featurize(features)
        p_win = self._predict(x)
        if p_win < ML_SKIP_THRESHOLD:
            return False, f"ML|P(win)={p_win:.2f}<{ML_SKIP_THRESHOLD}|SKIP"
        return True, f"ML|P(win)={p_win:.2f}|ENTER"

    def record_trade(self, features, pnl, source="live", entered=True, mae=0.0, mfe=0.0, **kwargs):
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
        self.trades.append({
            "features": features, "pnl": pnl, "win": 1 if pnl > 0 else 0,
            "entered": entered, "p_win": round(p, 3), "source": source,
            "timestamp": datetime.now(ET).isoformat(), "mae": mae, "mfe": mfe,
        })
        self.total_trades += 1
        self._save()
        wins = sum(1 for t in self.trades if t["win"] == 1)
        total = len(self.trades)
        print(f"[ML] Trade: PnL=${pnl:.2f} | P(win)={p:.2f} | {total}t {wins}w ({100*wins/total:.0f}%)")

    def stats(self):
        if not self.trades:
            return "No trades"
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"] == 1)
        total_pnl = sum(t["pnl"] for t in self.trades)
        return f"ML: {total}t W:{wins} L:{total-wins} {100*wins/total:.0f}% ${total_pnl:.2f}"


# ============================================================
# RL Parameter Optimizer
# ============================================================

RL_ACTIONS = [
    {"sl_pts": 10, "trail_activate": 40.0, "trail_pullback": 0.25, "dca_threshold": -150.0, "label": "tight"},
    {"sl_pts": 15, "trail_activate": 60.0, "trail_pullback": 0.30, "dca_threshold": -180.0, "label": "conservative"},
    {"sl_pts": 20, "trail_activate": 80.0, "trail_pullback": 0.35, "dca_threshold": -220.0, "label": "balanced"},
    {"sl_pts": 30, "trail_activate": 100.0, "trail_pullback": 0.40, "dca_threshold": -260.0, "label": "moderate"},
    {"sl_pts": 40, "trail_activate": 150.0, "trail_pullback": 0.45, "dca_threshold": -300.0, "label": "wide"},
    {"sl_pts": 50, "trail_activate": 200.0, "trail_pullback": 0.50, "dca_threshold": None, "label": "runner"},
]

RL_WARMUP = 20
RL_EPSILON_START = 0.30
RL_EPSILON_MIN = 0.10
RL_EPSILON_DECAY = 0.995
RL_LR = 0.10


class ParamRL:
    def __init__(self, data_file):
        self.data_file = data_file
        self.n_actions = len(RL_ACTIONS)
        self.q_table = {}
        self.epsilon = RL_EPSILON_START
        self.total_trades = 0
        self._load()

    def _state_key(self, features):
        consec = features.get("consecutive_bricks", 1)
        vol = "trending" if consec >= 8 else ("normal" if consec >= 3 else "choppy")
        mom = features.get("momentum_pts", 0.0)
        mom_s = "up" if mom > 2 else ("down" if mom < -2 else "flat")
        h = features.get("hour", 12)
        h_s = "off" if (h < 10 or h >= 18) else ("morning" if h < 12 else ("midday" if h < 14 else "afternoon"))
        return f"{vol}_{mom_s}_{h_s}"

    def choose(self, features):
        if self.total_trades < RL_WARMUP:
            return 2, RL_ACTIONS[2]
        key = self._state_key(features)
        if key not in self.q_table:
            self.q_table[key] = [0.0] * self.n_actions
        if random.random() < self.epsilon:
            idx = random.randint(0, self.n_actions - 1)
        else:
            q = self.q_table[key]
            idx = q.index(max(q))
        return idx, RL_ACTIONS[idx]

    def update(self, features, action_idx, reward):
        key = self._state_key(features)
        if key not in self.q_table:
            self.q_table[key] = [0.0] * self.n_actions
        old = self.q_table[key][action_idx]
        self.q_table[key][action_idx] = old + RL_LR * (reward - old)
        self.total_trades += 1
        self.epsilon = max(RL_EPSILON_MIN, self.epsilon * RL_EPSILON_DECAY)
        self._save()

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r") as f:
                    d = json.load(f)
                self.q_table = d.get("q_table", {})
                self.epsilon = d.get("epsilon", RL_EPSILON_START)
                self.total_trades = d.get("total_trades", 0)
                print(f"[RL] Loaded: {self.total_trades} trades, eps={self.epsilon:.3f}, "
                      f"{len(self.q_table)} states")
            except Exception:
                pass

    def _save(self):
        try:
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"q_table": self.q_table, "epsilon": self.epsilon,
                           "total_trades": self.total_trades}, f)
            os.replace(tmp, self.data_file)
        except Exception:
            pass


# ============================================================
# Signal Engine — single Renko/MFI/MSS instance
# ============================================================

class SignalEngine:
    """Processes ticks and produces trading signals. One instance shared across all accounts."""

    def __init__(self, symbol, data_dir):
        self.symbol = symbol
        self.pv = POINT_VALUES.get(symbol, 20.0)
        self.renko = RenkoBrickBuilder(BRICK_SIZE)
        self.candles = CandleBuilder(CANDLE_SECONDS)
        self.bb_candles = CandleBuilder(MSS_CANDLE_SECONDS)
        self.mss = MSSDetector()
        self.last_price = 0.0
        self.last_tick_time = 0.0
        self._prev_brick_dir = None
        self._pending_mss = None
        self._restart_ts = time.time()
        self._new_bricks_since_restart = 0
        self._restart_cooldown_done = False

        self._pending_bb_entry = None  # {"direction": "LONG"/"SHORT", "features": ..., "ts": ...}
        self._bb_candle_closes = []
        self._max_bb_candle_history = 500

        self.brick_closes = []
        self.brick_opens = []
        self.brick_typicals = []
        self.brick_volumes = []
        self.mfi_value = None
        self.prev_mfi_value = None
        self._max_brick_history = 250

        self.ml = TradeFilter(os.path.join(data_dir, f"ml_state_{symbol}.json"))
        self.rl = ParamRL(os.path.join(data_dir, f"rl_state_{symbol}.json"))

    def _add_brick_data(self, brick_open, brick_close):
        self.brick_closes.append(brick_close)
        self.brick_opens.append(brick_open)
        self.brick_volumes.append(1)
        h = max(brick_open, brick_close)
        l = min(brick_open, brick_close)
        self.brick_typicals.append((h + l + brick_close) / 3.0)
        if len(self.brick_closes) > self._max_brick_history:
            excess = len(self.brick_closes) - self._max_brick_history
            del self.brick_closes[:excess]
            del self.brick_opens[:excess]
            del self.brick_volumes[:excess]
            del self.brick_typicals[:excess]

    def _calc_mfi(self):
        n = len(self.brick_typicals)
        if n < MFI_PERIOD + 1:
            return None
        typicals = self.brick_typicals[-(MFI_PERIOD + 1):]
        volumes = self.brick_volumes[-(MFI_PERIOD + 1):]
        pos_flow, neg_flow = 0.0, 0.0
        for i in range(1, len(typicals)):
            raw_flow = typicals[i] * volumes[i]
            if typicals[i] > typicals[i - 1]:
                pos_flow += raw_flow
            elif typicals[i] < typicals[i - 1]:
                neg_flow += raw_flow
        if neg_flow < 1e-10:
            new_mfi = 100.0
        else:
            new_mfi = 100.0 - (100.0 / (1.0 + pos_flow / neg_flow))
        signal = None
        if self.prev_mfi_value is not None:
            if new_mfi <= MFI_OVERSOLD and self.prev_mfi_value > MFI_OVERSOLD:
                signal = "oversold"
            elif new_mfi >= MFI_OVERBOUGHT and self.prev_mfi_value < MFI_OVERBOUGHT:
                signal = "overbought"
        self.prev_mfi_value = self.mfi_value if self.mfi_value is not None else new_mfi
        self.mfi_value = new_mfi
        return signal

    def _calc_bb(self):
        """Calculate Bollinger Bands(20,2) on 30s candle closes to match TradingView.
        Returns (upper, middle, lower) or None if not enough data."""
        if len(self._bb_candle_closes) < BB_LENGTH:
            return None
        recent = self._bb_candle_closes[-BB_LENGTH:]
        middle = sum(recent) / BB_LENGTH
        variance = sum((c - middle) ** 2 for c in recent) / BB_LENGTH
        std = variance ** 0.5
        upper = middle + BB_MULT * std
        lower = middle - BB_MULT * std
        return upper, middle, lower

    def extract_features(self):
        return self.ml.extract_features(price=self.last_price, renko=self.renko)

    def tick(self, price, ts=None):
        """Process tick and return list of signal actions.
        Flow: MSS shift -> MFI exhaustion -> pending BB entry -> price touches BB band -> enter
        """
        if ts is None:
            ts = time.time()
        self.last_price = price
        self.last_tick_time = ts
        signals = []

        # Feed 30s candles for BB calculation
        completed_candle = self.candles.feed(price, ts)
        if completed_candle:
            self._bb_candle_closes.append(completed_candle["close"])
            if len(self._bb_candle_closes) > self._max_bb_candle_history:
                self._bb_candle_closes = self._bb_candle_closes[-self._max_bb_candle_history:]

        self.bb_candles.feed(price, ts)
        new_bricks = self.renko.feed(price)

        if new_bricks and not self._restart_cooldown_done:
            self._new_bricks_since_restart += len(new_bricks)
            elapsed = time.time() - self._restart_ts
            if self._new_bricks_since_restart >= 20 and elapsed >= 30:
                self._restart_cooldown_done = True
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} COOLDOWN-DONE] "
                      f"{self._new_bricks_since_restart} bricks in {elapsed:.0f}s — signals enabled")

        if new_bricks:
            for brick in new_bricks:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                self._add_brick_data(brick["open"], brick["close"])
                mfi_signal = self._calc_mfi()
                mfi_str = f"MFI={self.mfi_value:.1f}" if self.mfi_value is not None else "MFI warming"
                bb = self._calc_bb()
                bb_str = f"BB[{bb[2]:.1f}/{bb[1]:.1f}/{bb[0]:.1f}]" if bb else "BB warming"
                pend_str = f" | PENDING {self._pending_bb_entry['direction']}" if self._pending_bb_entry else ""
                print(f"[{now_str}] [{self.symbol} BRICK] {brick['direction'].upper()} "
                      f"{brick['open']:.2f} -> {brick['close']:.2f} "
                      f"(consecutive: {self.renko.consecutive_count()}) | {mfi_str} | {bb_str}{pend_str}")

                if self._restart_cooldown_done and mfi_signal and in_trade_session():
                    direction = "LONG" if mfi_signal == "oversold" else "SHORT"
                    matches_mss = ((self._pending_mss == "bullish" and direction == "LONG") or
                                   (self._pending_mss == "bearish" and direction == "SHORT"))
                    if matches_mss:
                        features = self.extract_features()
                        should_enter, reason = self.ml.should_enter(features)
                        print(f"[{now_str}] [{self.symbol} MFI-CONFIRM] {mfi_signal.upper()} "
                              f"-> {direction} | MSS {self._pending_mss} active | {reason}")
                        if should_enter:
                            self._pending_bb_entry = {
                                "direction": direction, "features": features,
                                "ts": time.time(), "mss": self._pending_mss,
                            }
                            self._pending_mss = None
                            self.mss.reset()
                            print(f"[{now_str}] [{self.symbol} BB-WAIT] {direction} armed — "
                                  f"waiting for price to touch {'upper' if direction == 'SHORT' else 'lower'} BB")
                    elif mfi_signal:
                        print(f"[{now_str}] [{self.symbol} MFI-DOT] {mfi_signal.upper()} "
                              f"-> {direction} | no matching MSS pending")

                self._prev_brick_dir = brick["direction"]

        # Check pending BB entry on every tick
        if self._pending_bb_entry and in_trade_session():
            bb = self._calc_bb()
            if bb:
                upper, middle, lower = bb
                direction = self._pending_bb_entry["direction"]
                if direction == "SHORT" and price >= upper:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} BB-TRIGGER] SHORT — "
                          f"price {price:.2f} >= upper BB {upper:.2f}")
                    features = self._pending_bb_entry["features"]
                    rl_idx, rl_params = self.rl.choose(features)
                    signals.append(("enter_short", price, features, rl_idx, rl_params))
                    self._pending_bb_entry = None
                elif direction == "LONG" and price <= lower:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} BB-TRIGGER] LONG — "
                          f"price {price:.2f} <= lower BB {lower:.2f}")
                    features = self._pending_bb_entry["features"]
                    rl_idx, rl_params = self.rl.choose(features)
                    signals.append(("enter_long", price, features, rl_idx, rl_params))
                    self._pending_bb_entry = None

        # MSS detection
        self.mss.update_swings(self.renko.bricks)
        if in_trade_session() and self._restart_cooldown_done:
            mss_signal = self.mss.check(price)
            if mss_signal and mss_signal != self._pending_mss:
                # Cancel pending BB entry if MSS flips opposite
                if self._pending_bb_entry:
                    pend_dir = self._pending_bb_entry["direction"]
                    opposite = (mss_signal == "bullish" and pend_dir == "SHORT") or \
                               (mss_signal == "bearish" and pend_dir == "LONG")
                    if opposite:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} BB-CANCEL] {pend_dir} pending cancelled — "
                              f"MSS flipped to {mss_signal}")
                        self._pending_bb_entry = None
                self._pending_mss = mss_signal
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                if mss_signal == "bearish":
                    print(f"[{now_str}] [{self.symbol} MSS-SHIFT] BEARISH @ {price:.2f} "
                          f"broke SL {self.mss.swing_lows[-1]:.2f} | waiting for MFI overbought -> SHORT")
                else:
                    print(f"[{now_str}] [{self.symbol} MSS-SHIFT] BULLISH @ {price:.2f} "
                          f"broke SH {self.mss.swing_highs[-1]:.2f} | waiting for MFI oversold -> LONG")

        return signals

    def save_state(self):
        return {
            "symbol": self.symbol, "saved_at": time.time(),
            "renko_last_close": self.renko._last_close,
            "renko_last_dir": self.renko._last_direction,
            "renko_bricks": self.renko.bricks[-50:],
            "prev_brick_dir": self._prev_brick_dir,
            "pending_mss": self._pending_mss,
            "pending_bb_entry": self._pending_bb_entry,
            "bb_candle_closes": self._bb_candle_closes[-200:],
            "brick_closes": self.brick_closes[-200:],
            "brick_opens": self.brick_opens[-200:],
            "brick_typicals": self.brick_typicals[-200:],
            "brick_volumes": self.brick_volumes[-200:],
            "mfi_value": self.mfi_value, "prev_mfi_value": self.prev_mfi_value,
            "candle_current": self.candles._current,
            "candle_history": self.candles.candles[-20:],
            "bb_candle_current": self.bb_candles._current,
            "bb_candle_history": self.bb_candles.candles[-30:],
        }

    def restore_state(self, state):
        renko_bricks = state.get("renko_bricks", [])
        if renko_bricks:
            self.renko.bricks = renko_bricks
            self.renko._last_close = state.get("renko_last_close")
            self.renko._last_direction = state.get("renko_last_dir")
        self._prev_brick_dir = state.get("prev_brick_dir")
        raw_mss = state.get("pending_mss")
        if isinstance(raw_mss, dict):
            self._pending_mss = raw_mss.get("dir")
        else:
            self._pending_mss = raw_mss
        self._pending_bb_entry = state.get("pending_bb_entry")
        self._bb_candle_closes = state.get("bb_candle_closes", [])
        self.brick_closes = state.get("brick_closes", [])
        self.brick_opens = state.get("brick_opens", [])
        self.brick_typicals = state.get("brick_typicals", [])
        self.brick_volumes = state.get("brick_volumes", [])
        self.mfi_value = state.get("mfi_value")
        self.prev_mfi_value = state.get("prev_mfi_value")
        cc = state.get("candle_current")
        if cc:
            self.candles._current = cc
        ch = state.get("candle_history", [])
        if ch:
            self.candles.candles = ch
        bcc = state.get("bb_candle_current")
        if bcc:
            self.bb_candles._current = bcc
        bch = state.get("bb_candle_history", [])
        if bch:
            self.bb_candles.candles = bch
        print(f"  [{self.symbol}] Signal engine restored: {len(self.renko.bricks)} bricks, "
              f"MFI={round(self.mfi_value or 0, 1)}, BB candles={len(self._bb_candle_closes)}, "
              f"pending_mss={self._pending_mss}, pending_bb={self._pending_bb_entry is not None}")


# ============================================================
# Account Connection — one per TopstepX account
# ============================================================

class AccountConnection:
    """Manages one TopstepX account: connection, position, PnL."""

    def __init__(self, name, username, api_key, account_name, symbol, base_qty,
                 tg_token="", tg_chat="", tg_keys=None, ntfy_topic="", tp_webhooks=None):
        self.name = name
        self.username = username
        self.api_key = api_key
        self.account_name = account_name
        self.symbol = symbol
        self.base_qty = base_qty
        self.pv = POINT_VALUES.get(symbol, 20.0)
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.ntfy_topic = ntfy_topic
        self.tp_webhooks = tp_webhooks or []

        self.suite = None
        self.ctx = None

        self.position = 0
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_features = None
        self._entry_prices = []
        self._dca_done = False
        self._rl_action_idx = None
        self._rl_params = None
        self._tp_lock_pnl = 0.0
        self.active_sl_pts = DEFAULT_SL_PTS
        self.trade_mae = 0.0
        self.trade_mfe = 0.0
        self._trail_profit_active = False
        self._trail_profit_peak = 0.0
        self._trail_profit_floor = 0.0
        self._pending_rl = None

        self.live_pnl = 0.0
        self.daily_loss = 0.0
        self._start_balance = None
        self._pnl_session_day = None

        self.connected = False
        self.config_file = None
        self._last_price_change_time = time.time()
        self._last_known_price = None
        self._last_position_poll = 0
        self._platform_flat_streak = 0

    def _update_accounts_json(self):
        """Write back new account_name to accounts.json after auto-switch."""
        if not self.config_file or not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, "r") as f:
                accounts = json.load(f)
            for acct in accounts:
                if acct.get("name") == self.name:
                    if acct.get("account_name") != self.account_name:
                        old = acct["account_name"]
                        acct["account_name"] = self.account_name
                        tmp = self.config_file + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(accounts, f, indent=2)
                        os.replace(tmp, self.config_file)
                        print(f"[{self.name}] accounts.json updated: {old} -> {self.account_name}")
                    break
        except Exception as e:
            print(f"[{self.name}] accounts.json update error: {e}")

    async def connect(self, symbols):
        from project_x_py import TradingSuite
        os.environ["PROJECT_X_USERNAME"] = self.username
        os.environ["PROJECT_X_API_KEY"] = self.api_key
        os.environ["PROJECT_X_ACCOUNT_NAME"] = self.account_name

        try:
            self.suite = await TradingSuite.create(
                instruments=symbols, timeframes=["1sec"], initial_days=1)
        except Exception as e:
            err_msg = str(e)
            if "not found" in err_msg:
                import re
                all_accts = re.findall(r'[A-Z0-9]+-V2-(?:[A-Z]+-)?[\d]+-[\d]+', err_msg)
                all_accts = [a for a in set(all_accts) if a != self.account_name]
                prac = [a for a in all_accts if a.startswith("PRAC-")]
                funded = [a for a in all_accts if a.startswith("50KTC-") and "-DLL-" not in a]
                candidates = prac or funded or all_accts
                if candidates:
                    new_acct = candidates[0]
                    print(f"[{self.name} AUTO-SWITCH] {self.account_name} -> {new_acct}")
                    self.account_name = new_acct
                    os.environ["PROJECT_X_ACCOUNT_NAME"] = new_acct
                    self.suite = await TradingSuite.create(
                        instruments=symbols, timeframes=["1sec"], initial_days=1)
                    self._update_accounts_json()
                else:
                    raise
            else:
                raise

        self.ctx = self.suite[self.symbol]
        self._register_handlers()
        self.connected = True
        self._last_price_change_time = time.time()
        self._last_known_price = None
        self._platform_flat_streak = 0
        print(f"[{self.name}] Connected: {self.account_name} | contract: {self.ctx.instrument_info.id}")

        await self._verify_position_on_connect()
        await self._sync_pnl_from_platform()

    def _register_handlers(self):
        conn = self.suite.realtime.user_connection
        _last_pos_event = {}

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

                if self.ctx and self.ctx.instrument_info.id == contract_id and self.position != 0:
                    price = getattr(self, '_last_price', 0.0)
                    direction = "LONG" if self.position == 1 else "SHORT"
                    if self.position == 1:
                        pnl_est = (price - self.entry_price) * self.pv * self.contracts_held
                    else:
                        pnl_est = (self.entry_price - price) * self.pv * self.contracts_held
                    self.live_pnl += pnl_est
                    self.position = 0
                    self.contracts_held = 0
                    print(f"[{self.name} WS] Platform closed {direction} — est PnL: ${pnl_est:.2f}")
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "FLAT", self.symbol, price, 0),
                        kwargs={"ntfy_topic": self.ntfy_topic, "tp_webhooks": self.tp_webhooks},
                        daemon=True).start()

                    rl_idx = self._rl_action_idx
                    feat = self.entry_features
                    mae, mfe = self.trade_mae, self.trade_mfe
                    self._rl_action_idx = None
                    self._rl_params = None
                    self._tp_lock_pnl = 0.0
            except Exception as e:
                print(f"[{self.name} WS] Position event error: {e}")

        conn.on("GatewayUserPosition", on_position_event)
        conn.on("PositionUpdate", on_position_event)

        def on_gateway_logout(*args):
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{self.name} WS] GatewayLogout received")
            self._gateway_logout = True

        conn.on("GatewayLogout", on_gateway_logout)
        self._gateway_logout = False

    async def reconcile_position(self):
        """Periodic safety check: verify bot position matches platform."""
        now_ts = time.time()
        if now_ts - self._last_position_poll < POSITION_POLL_INTERVAL:
            return
        self._last_position_poll = now_ts
        if not self.suite or not self.ctx:
            return
        try:
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            real_pos = 0
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    real_pos = p.size
                    break
            if self.position != 0 and real_pos == 0:
                self._platform_flat_streak += 1
                if self._platform_flat_streak >= PLATFORM_FLAT_THRESHOLD:
                    direction = "LONG" if self.position == 1 else "SHORT"
                    price = getattr(self, '_last_price', 0.0)
                    if self.position == 1:
                        pnl_est = (price - self.entry_price) * self.pv * self.contracts_held
                    else:
                        pnl_est = (self.entry_price - price) * self.pv * self.contracts_held
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.name} POS-SYNC] Platform closed {direction} "
                          f"externally. Est PnL: ${pnl_est:+.2f}")
                    self.live_pnl += pnl_est
                    self.position = 0
                    self.contracts_held = 0
                    self.entry_price = 0.0
                    self._platform_flat_streak = 0
                    threading.Thread(target=send_telegram,
                                     args=(self.tg_token, self.tg_chat,
                                           f"SYNC|{self.name} {direction} closed externally. "
                                           f"PnL: ${pnl_est:+.2f}"),
                                     daemon=True).start()
            else:
                self._platform_flat_streak = 0
        except Exception:
            pass

    async def _sync_entry_price_from_platform(self):
        if not self.suite or self.position == 0:
            return
        try:
            await asyncio.sleep(0.3)
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    old_avg = self.entry_price
                    self.entry_price = p.averagePrice
                    self.contracts_held = p.size
                    if abs(old_avg - p.averagePrice) > 0.01:
                        print(f"[{self.name} PRICE-SYNC] {old_avg:.2f} -> {p.averagePrice:.2f}")
                    break
        except Exception:
            pass

    async def _verify_position_on_connect(self):
        """After restart, verify restored position actually exists on the platform."""
        if not self.suite or self.position == 0:
            return
        try:
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            found = False
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    found = True
                    self.contracts_held = p.size
                    self.entry_price = p.averagePrice
                    direction = "LONG" if self.position == 1 else "SHORT"
                    print(f"[{self.name} POS-VERIFY] {direction} x{p.size} confirmed @ {p.averagePrice:.2f}")
                    break
            if not found:
                old_dir = "LONG" if self.position == 1 else "SHORT"
                print(f"[{self.name} POS-VERIFY] {old_dir} x{self.contracts_held} NOT FOUND on platform — clearing phantom position")
                self.position = 0
                self.contracts_held = 0
                self.entry_price = 0.0
                self.entry_time = 0.0
                self._dca_done = False
                self._entry_prices = []
                self._trail_profit_active = False
                self._trail_profit_peak = 0.0
                self._trail_profit_floor = 0.0
        except Exception as e:
            print(f"[{self.name} POS-VERIFY] Error: {e}")

    async def _sync_pnl_from_platform(self):
        if not self.suite or self.position != 0:
            return
        try:
            await asyncio.sleep(0.5)
            accounts = await asyncio.wait_for(
                self.suite.client.list_accounts(), timeout=8.0)
            for a in accounts:
                if a.name == self.account_name:
                    if self._start_balance is None:
                        self._start_balance = a.balance
                        self.live_pnl = 0.0
                        print(f"[{self.name} PNL-SYNC] Baseline: ${self._start_balance:,.2f}")
                    else:
                        real_pnl = a.balance - self._start_balance
                        drift = abs(real_pnl - self.live_pnl)
                        if drift > 2.0:
                            print(f"[{self.name} PNL-SYNC] ${self.live_pnl:.2f} -> ${real_pnl:.2f}")
                            self.live_pnl = real_pnl
                    break
        except Exception:
            pass

    async def _ensure_flat(self):
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=4.0)
        except Exception:
            pass

    async def enter_long(self, price, features, rl_idx, rl_params):
        if self.position != 0 or not self.connected:
            return False
        if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
            print(f"[{self.name}] Daily limit hit — skipping entry")
            return False
        await self._ensure_flat()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [{self.name}] >>> LONG x{qty} @ {price:.2f} | "
              f"TP=${DCA_TP_DOLLARS} | ({rl_params['label']}) | "
              f"trail=${rl_params['trail_activate']:.0f}/{100*rl_params['trail_pullback']:.0f}% | "
              f"DCA at -${abs(DCA_ADD_THRESHOLD)} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await asyncio.wait_for(asyncio.shield(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id, side=0, size=qty)),
                timeout=15.0)
            if response.success:
                self.position = 1
                self.contracts_held = qty
                self.entry_price = price
                self._entry_prices = [price]
                self._dca_done = False
                self.entry_time = time.time()
                self.entry_features = features
                self._rl_action_idx = rl_idx
                self._rl_params = rl_params
                self.active_sl_pts = rl_params["sl_pts"]
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._tp_lock_pnl = 0.0
                self._trail_profit_active = False
                self._trail_profit_peak = 0.0
                self._trail_profit_floor = 0.0
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "LONG", self.symbol, price, qty),
                    kwargs={"ntfy_topic": self.ntfy_topic, "tp_webhooks": self.tp_webhooks},
                    daemon=True).start()
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"[{self.name}] Order FAILED: {response}")
                return False
        except asyncio.TimeoutError:
            print(f"[{self.name}] Order TIMEOUT")
            return False
        except Exception as e:
            print(f"[{self.name}] Order ERROR: {e}")
            return False

    async def enter_short(self, price, features, rl_idx, rl_params):
        if self.position != 0 or not self.connected:
            return False
        if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
            print(f"[{self.name}] Daily limit hit — skipping entry")
            return False
        await self._ensure_flat()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [{self.name}] >>> SHORT x{qty} @ {price:.2f} | "
              f"TP=${DCA_TP_DOLLARS} | ({rl_params['label']}) | "
              f"trail=${rl_params['trail_activate']:.0f}/{100*rl_params['trail_pullback']:.0f}% | "
              f"DCA at -${abs(DCA_ADD_THRESHOLD)} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await asyncio.wait_for(asyncio.shield(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id, side=1, size=qty)),
                timeout=15.0)
            if response.success:
                self.position = -1
                self.contracts_held = qty
                self.entry_price = price
                self._entry_prices = [price]
                self._dca_done = False
                self.entry_time = time.time()
                self.entry_features = features
                self._rl_action_idx = rl_idx
                self._rl_params = rl_params
                self.active_sl_pts = rl_params["sl_pts"]
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._tp_lock_pnl = 0.0
                self._trail_profit_active = False
                self._trail_profit_peak = 0.0
                self._trail_profit_floor = 0.0
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "SHORT", self.symbol, price, qty),
                    kwargs={"ntfy_topic": self.ntfy_topic, "tp_webhooks": self.tp_webhooks},
                    daemon=True).start()
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"[{self.name}] Order FAILED: {response}")
                return False
        except asyncio.TimeoutError:
            print(f"[{self.name}] Order TIMEOUT")
            return False
        except Exception as e:
            print(f"[{self.name}] Order ERROR: {e}")
            return False

    async def dca_add(self, price):
        if not self.connected:
            return False
        qty = self.base_qty
        side = 0 if self.position == 1 else 1
        direction = "LONG" if self.position == 1 else "SHORT"
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [{self.name} DCA] {direction} x{qty} @ {price:.2f} | "
              f"Now {self.contracts_held + qty} contracts")
        try:
            response = await asyncio.wait_for(asyncio.shield(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id, side=side, size=qty)),
                timeout=15.0)
            if response.success:
                self._entry_prices.append(price)
                old_total = self.entry_price * self.contracts_held
                self.contracts_held += qty
                self.entry_price = (old_total + price * qty) / self.contracts_held
                self._dca_done = True
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"[{self.name} DCA] FAILED: {response}")
                return False
        except Exception as e:
            print(f"[{self.name} DCA] ERROR: {e}")
            return False

    async def flatten(self, price, reason="signal", signal_engine=None):
        if self.position == 0 or not self.connected:
            return
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl_before = self.live_pnl

        try:
            await asyncio.wait_for(asyncio.shield(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id)),
                timeout=15.0)
        except asyncio.TimeoutError:
            print(f"[{self.name}] CLOSE TIMEOUT — position may still be open!")
            return
        except Exception as e:
            print(f"[{self.name}] CLOSE ERROR: {e}")
            return

        if self.position == 1:
            pts = price - self.entry_price
        else:
            pts = self.entry_price - price
        trade_pnl = pts * self.pv * self.contracts_held
        self.live_pnl += trade_pnl
        self.daily_loss += min(0, trade_pnl)

        print(f"[{now_str}] [{self.name}] <<< EXIT {direction} x{self.contracts_held} "
              f"@ {price:.2f} | Trade: ${trade_pnl:.2f} | Session: ${self.live_pnl:.2f} | {reason}")

        rl_idx = self._rl_action_idx
        rl_feat = self.entry_features
        mae = self.trade_mae
        mfe = self.trade_mfe

        self.position = 0
        self.contracts_held = 0
        self._trail_profit_active = False
        self._trail_profit_peak = 0.0
        self._trail_profit_floor = 0.0
        self._tp_lock_pnl = 0.0

        threading.Thread(target=send_signals, args=(
            self.tg_token, self.tg_chat, self.tg_keys,
            "FLAT", self.symbol, price, 0),
            kwargs={"ntfy_topic": self.ntfy_topic, "tp_webhooks": self.tp_webhooks},
            daemon=True).start()

        await self._sync_pnl_from_platform()
        real_trade_pnl = self.live_pnl - pnl_before
        if abs(real_trade_pnl - trade_pnl) > 2.0:
            trade_pnl = real_trade_pnl

        if signal_engine and rl_feat:
            signal_engine.ml.record_trade(rl_feat, trade_pnl, source="live",
                                           entered=True, mae=mae, mfe=mfe)
            if rl_idx is not None:
                signal_engine.rl.update(rl_feat, rl_idx, trade_pnl)

        self.entry_features = None
        self._rl_action_idx = None
        self._rl_params = None
        self._pending_rl = None

    def check_position(self, price):
        """Check position-level exits: TP, trailing, DCA. Returns list of actions."""
        if self.position == 0:
            return []

        if self.position == 1:
            unrealized = (price - self.entry_price) * self.pv * self.contracts_held
        else:
            unrealized = (self.entry_price - price) * self.pv * self.contracts_held
        self.trade_mfe = max(self.trade_mfe, unrealized)
        self.trade_mae = min(self.trade_mae, unrealized)

        trail_act = self._rl_params["trail_activate"] if self._rl_params else TRAIL_PROFIT_ACTIVATE
        trail_pb = self._rl_params["trail_pullback"] if self._rl_params else TRAIL_PROFIT_PULLBACK

        # Trailing profit
        if unrealized >= trail_act:
            if not self._trail_profit_active:
                self._trail_profit_active = True
                self._trail_profit_peak = unrealized
                self._trail_profit_floor = unrealized * (1.0 - trail_pb)
                direction = "LONG" if self.position == 1 else "SHORT"
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.name} TRAIL] {direction} activated ${unrealized:.0f} | "
                      f"floor=${self._trail_profit_floor:.0f}")
            elif unrealized > self._trail_profit_peak:
                self._trail_profit_peak = unrealized
                self._trail_profit_floor = unrealized * (1.0 - trail_pb)

        if self._trail_profit_active and unrealized <= self._trail_profit_floor:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{self.name} TRAIL-EXIT] ${unrealized:.0f} <= floor ${self._trail_profit_floor:.0f}")
            return [("flatten", price, "TRAIL_PROFIT")]

        # TP
        if unrealized >= DCA_TP_DOLLARS:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            print(f"[{now_str}] [{self.name} TP-HIT] {direction} x{self.contracts_held} "
                  f"${unrealized:.0f} >= ${DCA_TP_DOLLARS} — market exit")
            return [("flatten", price, "TP")]

        # DCA
        dca_thresh = self._rl_params.get("dca_threshold") if self._rl_params else DCA_ADD_THRESHOLD
        if dca_thresh is not None and not self._dca_done and self.contracts_held < DCA_MAX_CONTRACTS:
            if unrealized <= dca_thresh:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.name} DCA-TRIGGER] ${unrealized:.0f} <= ${dca_thresh:.0f}")
                return [("dca_add", price)]

        return []

    def check_session_reset(self):
        now_et = datetime.now(ET)
        session_day = now_et.date() if now_et.time() >= SESSION_START else now_et.date() - timedelta(days=1)
        if self._pnl_session_day is None or self._pnl_session_day != session_day:
            self._pnl_session_day = session_day
            self._start_balance = None
            self.live_pnl = 0.0
            self.daily_loss = 0.0
            print(f"[{self.name} SESSION] New day {session_day} — reset")

    def save_state(self):
        return {
            "name": self.name, "account_name": self.account_name, "saved_at": time.time(),
            "position": self.position, "contracts_held": self.contracts_held,
            "entry_price": self.entry_price, "entry_time": self.entry_time,
            "entry_features": self.entry_features, "active_sl_pts": self.active_sl_pts,
            "live_pnl": self.live_pnl, "trade_mae": self.trade_mae, "trade_mfe": self.trade_mfe,
            "trail_profit_active": self._trail_profit_active,
            "trail_profit_peak": self._trail_profit_peak,
            "trail_profit_floor": self._trail_profit_floor,
            "daily_loss": self.daily_loss, "dca_done": self._dca_done,
            "entry_prices": self._entry_prices,
            "rl_action_idx": self._rl_action_idx, "rl_params": self._rl_params,
            "tp_lock_pnl": self._tp_lock_pnl,
        }

    def restore_state(self, state, position_ttl=600):
        age = time.time() - state.get("saved_at", 0)
        self.daily_loss = state.get("daily_loss", 0.0)
        self._dca_done = state.get("dca_done", False)
        self._entry_prices = state.get("entry_prices", [])
        self._rl_action_idx = state.get("rl_action_idx")
        self._rl_params = state.get("rl_params")
        self._tp_lock_pnl = state.get("tp_lock_pnl", 0.0)

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
            print(f"  [{self.name}] Restored: pos={self.position}, P&L=${self.live_pnl:.2f}")
            return True
        else:
            print(f"  [{self.name}] State too old ({age:.0f}s), cleared")
            return False


# ============================================================
# Multi-Account Bot
# ============================================================

class MultiAccountBot:
    def __init__(self, accounts_config, symbol, base_qty,
                 tg_token="", tg_chat="", tg_keys=None, tp_webhooks=None,
                 data_dir=".", config_file=None):
        self.symbol = symbol
        self.base_qty = base_qty
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tp_webhooks = tp_webhooks or []
        self.running = True
        self.data_dir = data_dir
        self.config_file = config_file
        self.state_file = os.path.join(data_dir, "bot_state_multi.json")

        self.engine = SignalEngine(symbol, data_dir)
        self.accounts = []
        for acfg in accounts_config:
            acc = AccountConnection(
                name=acfg["name"],
                username=acfg["username"],
                api_key=acfg["api_key"],
                account_name=acfg["account_name"],
                symbol=symbol,
                base_qty=base_qty,
                tg_token=tg_token,
                tg_chat=tg_chat,
                tg_keys=self.tg_keys,
                ntfy_topic=acfg.get("ntfy_topic", ""),
                tp_webhooks=self.tp_webhooks,
            )
            acc.config_file = config_file
            self.accounts.append(acc)

        self.last_tick_time = time.time()
        self.last_real_tick_time = 0.0

        self._reconnecting = False
        self._reconnect_failures = 0
        self._reconnect_cooldown = RECONNECT_COOLDOWN_BASE
        self._last_reconnect_time = 0
        self._last_heartbeat = 0

    def save_all_state(self):
        try:
            state = {
                "engine": self.engine.save_state(),
                "accounts": {a.name: a.save_state() for a in self.accounts},
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[STATE] Save error: {e}")

    def load_all_state(self):
        if not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            if "engine" in state:
                self.engine.restore_state(state["engine"])
            acct_states = state.get("accounts", {})
            for a in self.accounts:
                if a.name in acct_states:
                    a.restore_state(acct_states[a.name])
            return True
        except Exception as e:
            print(f"[STATE] Load error: {e}")
            return False

    async def connect_all(self):
        symbols = [self.symbol]
        for acc in self.accounts:
            try:
                await acc.connect(symbols)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"[{acc.name}] Connection FAILED: {e}")
                acc.connected = False

        connected = [a for a in self.accounts if a.connected]
        print(f"[BOT] Connected {len(connected)}/{len(self.accounts)} accounts")
        if not connected:
            raise RuntimeError("No accounts connected")

    async def _auto_reconnect(self):
        """Reconnect all accounts with exponential backoff, preserve indicators."""
        self._reconnecting = True
        self._last_reconnect_time = time.time()
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now_str}] [RECONNECT] attempt #{self._reconnect_failures + 1}, indicators preserved...")
        threading.Thread(target=send_telegram,
                         args=(self.tg_token, self.tg_chat,
                               f"STATUS|Auto-reconnecting ({now_str} ET)"),
                         daemon=True).start()
        for acc in self.accounts:
            if acc.connected and acc.suite:
                try:
                    await acc.suite.disconnect()
                except Exception:
                    pass
                acc.connected = False
        await asyncio.sleep(2)
        try:
            await self.connect_all()
            self._reconnect_failures = 0
            self._reconnect_cooldown = RECONNECT_COOLDOWN_OK
            self.last_real_tick_time = time.time()
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [RECONNECT] WebSocket restored")
            threading.Thread(target=send_telegram,
                             args=(self.tg_token, self.tg_chat,
                                   f"STATUS|RECONNECTED ({now_str} ET)"),
                             daemon=True).start()
        except Exception as e:
            self._reconnect_failures += 1
            self._reconnect_cooldown = min(
                RECONNECT_COOLDOWN_BASE * (2 ** (self._reconnect_failures - 1)),
                RECONNECT_COOLDOWN_MAX)
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [RECONNECT] Failed (#{self._reconnect_failures}): {e} — "
                  f"retry in {self._reconnect_cooldown}s")
        finally:
            self._reconnecting = False

    def _send_heartbeat(self):
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        acct_lines = []
        for acc in self.accounts:
            if not acc.connected:
                acct_lines.append(f"  {acc.name}: DISCONNECTED")
                continue
            pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[acc.position]
            acct_lines.append(f"  {acc.name}: {pos_str} x{acc.contracts_held} | ${acc.live_pnl:.2f}")
        mfi_str = f"MFI={self.engine.mfi_value:.1f}" if self.engine.mfi_value else "MFI=?"
        msg = (f"HEARTBEAT|{now_str} ET\n"
               f"{mfi_str} | Bricks: {len(self.engine.renko.bricks)}\n"
               + "\n".join(acct_lines))
        threading.Thread(target=send_telegram,
                         args=(self.tg_token, self.tg_chat, msg),
                         daemon=True).start()

    async def _broadcast_entry(self, direction, price, features, rl_idx, rl_params):
        """Place entry on ALL accounts simultaneously."""
        tasks = []
        for acc in self.accounts:
            if not acc.connected or acc.position != 0:
                continue
            if abs(acc.daily_loss) >= DAILY_LOSS_LIMIT:
                continue
            if direction == "LONG":
                tasks.append(acc.enter_long(price, features, rl_idx, rl_params))
            else:
                tasks.append(acc.enter_short(price, features, rl_idx, rl_params))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _broadcast_flatten(self, direction_filter, price, reason):
        """Flatten ALL accounts matching direction simultaneously."""
        tasks = []
        for acc in self.accounts:
            if not acc.connected or acc.position == 0:
                continue
            acct_dir = "LONG" if acc.position == 1 else "SHORT"
            if direction_filter and acct_dir != direction_filter:
                continue
            tasks.append(acc.flatten(price, reason, signal_engine=self.engine))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        print(f"[BOT] Multi-Account Renko MFI+MSS Bot")
        print(f"[BOT] Brick: {BRICK_SIZE}pt | MFI({MFI_PERIOD}) OB={MFI_OVERBOUGHT}/OS={MFI_OVERSOLD}")
        print(f"[BOT] TP=${DCA_TP_DOLLARS} | DCA at -${abs(DCA_ADD_THRESHOLD)} | max {DCA_MAX_CONTRACTS} contracts")
        print(f"[BOT] Session: {TRADE_SESSION_START.strftime('%H:%M')} - "
              f"{TRADE_SESSION_END.strftime('%H:%M')} ET")
        print(f"[BOT] Accounts: {[a.name for a in self.accounts]}")

        self.load_all_state()
        await self.connect_all()

        # Use first connected account for price feed
        primary_acc = next((a for a in self.accounts if a.connected), None)
        if not primary_acc:
            raise RuntimeError("No connected accounts for price feed")

        price = await primary_acc.ctx.data.get_current_price()
        print(f"[BOT] {self.symbol} price: {price:.2f}" if price else f"[BOT] {self.symbol}: market closed")

        acct_list = ", ".join(f"{a.name}({a.account_name})" for a in self.accounts if a.connected)
        msg = (f"STATUS|Multi-Account Bot started\n"
               f"Accounts: {acct_list}\n"
               f"Brick: {BRICK_SIZE}pt | TP: ${DCA_TP_DOLLARS}")
        threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat, msg),
                         daemon=True).start()

        self.last_tick_time = time.time()
        self.last_real_tick_time = time.time()
        self._reconnect_failures = 0
        self._reconnect_cooldown = RECONNECT_COOLDOWN_BASE
        self._last_heartbeat = time.time()
        _last_seen_price = None
        _was_in_session = False
        last_save = time.time()
        last_status = time.time()
        last_gc = time.time()
        last_acct_check = time.time()

        while self.running:
            if not in_session():
                await asyncio.sleep(5)
                self.last_tick_time = time.time()
                _was_in_session = False
                continue

            if in_blackout():
                await asyncio.sleep(1)
                self.last_tick_time = time.time()
                _was_in_session = False
                continue

            if not _was_in_session:
                self.last_real_tick_time = time.time()
                self._reconnect_failures = 0
                self._reconnect_cooldown = RECONNECT_COOLDOWN_BASE
                _last_seen_price = None
                _was_in_session = True
                for acc in self.accounts:
                    acc._last_price_change_time = time.time()
                print(f"[SESSION] Active — monitoring")

            # Get price from first connected account
            price = None
            for acc in self.accounts:
                if not acc.connected:
                    continue
                try:
                    price = await asyncio.wait_for(
                        acc.ctx.data.get_current_price(), timeout=5.0)
                    if price and price > 0:
                        break
                except Exception:
                    continue

            if price is None or price <= 0:
                await asyncio.sleep(1)
                continue

            self.last_tick_time = time.time()
            price_changed = (_last_seen_price is None or price != _last_seen_price)
            if price_changed:
                _last_seen_price = price
                self.last_real_tick_time = time.time()
                for acc in self.accounts:
                    acc._last_price_change_time = time.time()
                    acc._last_known_price = price

            # Update all accounts' last price (for WS handler)
            for acc in self.accounts:
                acc._last_price = price
                acc.check_session_reset()

            # 1. Check per-account position exits (TP, trail, DCA)
            for acc in self.accounts:
                if not acc.connected:
                    continue
                pos_actions = acc.check_position(price)
                for act in pos_actions:
                    if act[0] == "flatten":
                        await acc.flatten(act[1], act[2], signal_engine=self.engine)
                    elif act[0] == "dca_add":
                        await acc.dca_add(act[1])

            # 2. Feed signal engine and get entry/exit signals
            signals = self.engine.tick(price, self.last_tick_time)

            for sig in signals:
                if sig[0] == "enter_long":
                    _, ep, features, rl_idx, rl_params = sig
                    # Check if any account already has a position
                    any_in_position = any(a.position != 0 for a in self.accounts if a.connected)
                    if not any_in_position:
                        await self._broadcast_entry("LONG", ep, features, rl_idx, rl_params)
                elif sig[0] == "enter_short":
                    _, ep, features, rl_idx, rl_params = sig
                    any_in_position = any(a.position != 0 for a in self.accounts if a.connected)
                    if not any_in_position:
                        await self._broadcast_entry("SHORT", ep, features, rl_idx, rl_params)

            now = time.time()

            # --- Frozen-feed detection ---
            need_reconnect = False
            for acc in self.accounts:
                if not acc.connected:
                    continue
                feed_age = now - acc._last_price_change_time
                if feed_age > FROZEN_FEED_THRESHOLD and in_session() and not in_blackout():
                    print(f"[{acc.name}] FROZEN FEED: no price change for {feed_age:.0f}s")
                    need_reconnect = True
                    break
                if acc._gateway_logout:
                    print(f"[{acc.name}] GatewayLogout received — reconnecting")
                    acc._gateway_logout = False
                    need_reconnect = True
                    break

            # --- Stale data bailout ---
            tick_gap = now - self.last_real_tick_time
            if not need_reconnect and tick_gap > STALE_DATA_THRESHOLD and in_session() and not in_blackout():
                print(f"[HEALTH] Global stale: no real tick for {tick_gap:.0f}s")
                need_reconnect = True

            # --- Auto-reconnect with exponential backoff ---
            if need_reconnect and not self._reconnecting:
                cooldown_ok = (now - self._last_reconnect_time) > self._reconnect_cooldown
                if cooldown_ok:
                    await self._auto_reconnect()
                    _last_seen_price = None
                    continue

            # --- Position reconciliation (periodic HTTP poll) ---
            for acc in self.accounts:
                if acc.connected and acc.position != 0:
                    await acc.reconcile_position()

            # --- Heartbeat ---
            if now - self._last_heartbeat > HEARTBEAT_INTERVAL:
                self._send_heartbeat()
                self._last_heartbeat = now

            # --- Periodic save ---
            if now - last_save > 15:
                self.save_all_state()
                last_save = now

            # --- Status log ---
            if now - last_status > 300:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                brick_str = (f"Last brick: {self.engine.renko.last_direction() or 'none'} "
                             f"@ {self.engine.renko._last_close:.2f}") if self.engine.renko._last_close else "No bricks"
                mfi_str = f"MFI={self.engine.mfi_value:.1f}" if self.engine.mfi_value else "MFI=?"
                print(f"\n  [{self.symbol} @ {now_str}] Price: {price:.2f} | {brick_str} | {mfi_str}")
                print(f"    Bricks: {len(self.engine.renko.bricks)} | {self.engine.ml.stats()}")
                for acc in self.accounts:
                    if not acc.connected:
                        continue
                    pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[acc.position]
                    print(f"    [{acc.name}] {pos_str} x{acc.contracts_held} | P&L: ${acc.live_pnl:.2f}")
                last_status = now

            # --- GC + memory ---
            if now - last_gc > 120:
                gc.collect()
                import resource
                rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] [MEM] RSS: {rss_mb:.0f}MB")
                last_gc = now

            # --- PRAC account auto-switch check ---
            if now - last_acct_check > 60:
                last_acct_check = now
                for acc in self.accounts:
                    if not acc.connected or not acc.suite:
                        continue
                    if "PRAC" not in acc.account_name.upper():
                        continue
                    try:
                        accounts = await asyncio.wait_for(
                            acc.suite.client.list_accounts(), timeout=10.0)
                        acct_names = [a.name for a in accounts]
                        if acc.account_name not in acct_names:
                            prac = [n for n in acct_names if "PRAC" in n.upper()]
                            print(f"[{acc.name} ACCT-CHECK] {acc.account_name} gone! Available: {acct_names}")
                            if prac:
                                print(f"[{acc.name}] Restarting to switch to {prac[0]}")
                            self.save_all_state()
                            os._exit(1)
                    except Exception:
                        pass

            # --- Watchdog ---
            if time.time() - self.last_tick_time > WATCHDOG_TIMEOUT:
                print(f"[WATCHDOG] No ticks for {time.time() - self.last_tick_time:.0f}s — killing")
                self.save_all_state()
                import sys
                sys.exit(1)

            await asyncio.sleep(1)


# ============================================================
# Entry Point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Multi-Account Renko MFI+MSS Bot")
    parser.add_argument("--config", required=True, help="JSON config file with account credentials")
    parser.add_argument("--symbols", default="NQ:1", help="Symbol:qty")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--tp-webhooks", default="", help="Comma-separated TradersPost URLs")
    parser.add_argument("--data-dir", default=".", help="Directory for ML/RL/state files")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        accounts_config = json.load(f)

    parts = args.symbols.strip().split(":")
    symbol = parts[0].upper()
    base_qty = int(parts[1]) if len(parts) > 1 else 1

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []
    tp_webhooks = [u.strip() for u in args.tp_webhooks.split(",") if u.strip()] if args.tp_webhooks else []

    print(f"[BOT] Multi-Account Renko MFI+MSS Bot (ML + RL)")
    print(f"[BOT] Brick: {BRICK_SIZE}pt | MFI({MFI_PERIOD}) OB={MFI_OVERBOUGHT}/OS={MFI_OVERSOLD}")
    print(f"[BOT] Accounts: {[a['name'] for a in accounts_config]}")
    print(f"[BOT] Symbol: {symbol} x{base_qty}")

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
                    print(f"[WATCHDOG] No real ticks for {real_gap:.0f}s — force-killing")
                    current_bot.save_all_state()
                    os._exit(1)
            elif loop_gap > WATCHDOG_TIMEOUT + 120:
                print(f"[WATCHDOG] Event loop dead ({loop_gap:.0f}s) — force-killing")
                current_bot.save_all_state()
                os._exit(1)

    wd = threading.Thread(target=thread_watchdog, daemon=True)
    wd.start()

    retry_delay = 30
    while not stopped:
        bot = MultiAccountBot(
            accounts_config=accounts_config,
            symbol=symbol,
            base_qty=base_qty,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            tp_webhooks=tp_webhooks,
            data_dir=args.data_dir,
            config_file=args.config,
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
                          f"STATUS|Multi-account bot crashed, restarting in {retry_delay}s")
            retry_delay = min(retry_delay * 2, 300)
        finally:
            bot.save_all_state()
            loop.close()

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
