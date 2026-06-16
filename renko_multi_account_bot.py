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

try:
    from project_x_py.models import Position as _Position
    _orig_init = _Position.__init__
    def _patched_init(self, **kwargs):
        known = {"id", "accountId", "contractId", "creationTimestamp", "type", "size", "averagePrice"}
        filtered = {k: v for k, v in kwargs.items() if k in known}
        _orig_init(self, **filtered)
    _Position.__init__ = _patched_init
except Exception:
    pass


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
TP_SLIPPAGE_BUFFER = 20.0
DCA_ADD_THRESHOLD = -220.0
DCA_MAX_CONTRACTS = 1

TRAIL_PROFIT_ACTIVATE = 60.0
TRAIL_PROFIT_PULLBACK = 0.40

FLIP_NET_TP = 100.0
FLIP_BUFFER_PTS = 3.0
FLIP_COOLDOWN_SECS = 30

SHADOW_THRESHOLDS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]

CHANNEL_LOOKBACK = 120
CHANNEL_SLOPE_MIN = 0.015

VP_LOOKBACK_CANDLES = 480   # 480 x 30s = 4 hours rolling window
VP_BIN_SIZE = 2.0           # 2-point price bins
VP_HVN_PERCENTILE = 0.60   # top 40% of bins = high volume node

HULL_LENGTH = 24
HULL_FRESH_CANDLES = 3  # only block if Hull flipped within last 3 candles (90s)

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
FROZEN_FEED_THRESHOLD = 60
STALE_DATA_THRESHOLD = 150
HEARTBEAT_INTERVAL = 1800
POSITION_POLL_INTERVAL = 30
PLATFORM_FLAT_THRESHOLD = 3
MAX_BRICKS_PER_FEED = 10000

MSS_SWING_LOOKBACK = 5
MSS_MIN_SWING_PTS = 2.0
MSS_WARMUP_BRICKS = 15
MSS_MIN_PERSIST_BRICKS = 12

BB_LENGTH = 20
BB_MULT = 2.0
BB_SLOPE_LOOKBACK = 5
BB_SLOPE_MAX = 50.0


def ordinal(n):
    return f"{n}{'th' if 11<=n%100<=13 else {1:'st',2:'nd',3:'rd'}.get(n%10,'th')}"

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

RL_TP_ACTIONS = [
    {"tp_dollars": 100, "sl_dollars": 0,   "label": "tp100-nosl"},
    {"tp_dollars": 150, "sl_dollars": 50,  "label": "tp150-sl50"},
    {"tp_dollars": 150, "sl_dollars": 100, "label": "tp150-sl100"},
    {"tp_dollars": 200, "sl_dollars": 75,  "label": "tp200-sl75"},
    {"tp_dollars": 200, "sl_dollars": 125, "label": "tp200-sl125"},
    {"tp_dollars": 250, "sl_dollars": 100, "label": "tp250-sl100"},
    {"tp_dollars": 250, "sl_dollars": 150, "label": "tp250-sl150"},
    {"tp_dollars": 300, "sl_dollars": 100, "label": "tp300-sl100"},
    {"tp_dollars": 300, "sl_dollars": 150, "label": "tp300-sl150"},
    {"tp_dollars": 200, "sl_dollars": 0,   "label": "tp200-nosl"},
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
        pavp = features.get("pavp_zone", "")
        if pavp:
            return f"{vol}_{mom_s}_{h_s}_{pavp}"
        return f"{vol}_{mom_s}_{h_s}"

    def choose(self, features):
        # RL disabled for now - always use balanced params. Still records data for future use.
        return 0, RL_ACTIONS[0]

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


class TpSlRL:
    """RL optimizer for TP/SL dollar amounts. Epsilon-greedy Q-learning."""
    def __init__(self, data_file):
        self.data_file = data_file
        self.n_actions = len(RL_TP_ACTIONS)
        self.q_table = {}
        self.epsilon = 0.30
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
        if self.total_trades < 20:
            idx = random.randint(0, self.n_actions - 1)
            return idx, RL_TP_ACTIONS[idx]
        if random.random() < self.epsilon:
            idx = random.randint(0, self.n_actions - 1)
            return idx, RL_TP_ACTIONS[idx]
        key = self._state_key(features)
        if key in self.q_table:
            idx = max(range(self.n_actions), key=lambda i: self.q_table[key][i])
            return idx, RL_TP_ACTIONS[idx]
        return 0, RL_TP_ACTIONS[0]

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
                self.epsilon = d.get("epsilon", 0.30)
                self.total_trades = d.get("total_trades", 0)
                print(f"[RL-TP] Loaded: {self.total_trades} trades, eps={self.epsilon:.3f}, "
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

class TrendLineDetector:
    """LH-LL / HL-HH trend detection from 30s candles. Detects trend direction and break."""

    def __init__(self):
        self._highs = []
        self._lows = []
        self._trend = ""
        self._trend_line_price = None
        self._trend_line_slope = 0.0
        self._trend_start_idx = 0
        self._trend_start_price = 0.0
        self._candle_idx = 0

    def feed_candle(self, candle):
        self._candle_idx += 1
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        is_bearish = c < o
        is_bullish = c > o
        if is_bearish and self._highs and len(self._highs) >= 1:
            prev_candles = self._highs[-5:] if len(self._highs) >= 5 else self._highs
            if any(pc.get("bullish") for pc in prev_candles):
                self._highs.append({"idx": self._candle_idx, "price": h, "close": c, "bullish": False})
                if len(self._highs) > 20:
                    self._highs = self._highs[-20:]
        elif is_bullish:
            self._highs.append({"idx": self._candle_idx, "price": h, "close": c, "bullish": True})
            if len(self._highs) > 20:
                self._highs = self._highs[-20:]
        if is_bullish and self._lows and len(self._lows) >= 1:
            prev_candles = self._lows[-5:] if len(self._lows) >= 5 else self._lows
            if any(pc.get("bearish") for pc in prev_candles):
                self._lows.append({"idx": self._candle_idx, "price": l, "close": c, "bearish": False})
                if len(self._lows) > 20:
                    self._lows = self._lows[-20:]
        elif is_bearish:
            self._lows.append({"idx": self._candle_idx, "price": l, "close": c, "bearish": True})
            if len(self._lows) > 20:
                self._lows = self._lows[-20:]
        self._detect_trend(c)

    def _detect_trend(self, current_close):
        old_trend = self._trend
        lows = [x for x in self._lows if not x.get("bearish", True)]
        highs = [x for x in self._highs if not x.get("bullish", True)]
        if len(lows) >= 2:
            if lows[-1]["price"] > lows[-2]["price"]:
                if self._trend != "u":
                    self._trend_start_idx = lows[-2]["idx"]
                    self._trend_start_price = lows[-2]["price"]
                self._trend = "u"
                slope = (lows[-1]["price"] - self._trend_start_price) / max(1, lows[-1]["idx"] - self._trend_start_idx)
                self._trend_line_slope = slope
                self._trend_line_price = self._trend_start_price + slope * (self._candle_idx - self._trend_start_idx)
        if len(highs) >= 2:
            if highs[-1]["price"] < highs[-2]["price"]:
                if self._trend != "d":
                    self._trend_start_idx = highs[-2]["idx"]
                    self._trend_start_price = highs[-2]["price"]
                self._trend = "d"
                slope = (highs[-1]["price"] - self._trend_start_price) / max(1, highs[-1]["idx"] - self._trend_start_idx)
                self._trend_line_slope = slope
                self._trend_line_price = self._trend_start_price + slope * (self._candle_idx - self._trend_start_idx)
        if self._trend == "u" and self._trend_line_price and current_close < self._trend_line_price:
            self._trend = ""
            self._trend_line_price = None
        elif self._trend == "d" and self._trend_line_price and current_close > self._trend_line_price:
            self._trend = ""
            self._trend_line_price = None
        return old_trend != self._trend

    def get_trend(self):
        return self._trend

    def get_trend_line_price(self):
        return self._trend_line_price

    def trend_just_broke(self, old_trend):
        return old_trend != "" and self._trend == ""

    def save_state(self):
        return {
            "highs": self._highs[-10:], "lows": self._lows[-10:],
            "trend": self._trend, "trend_line_price": self._trend_line_price,
            "trend_line_slope": self._trend_line_slope,
            "trend_start_idx": self._trend_start_idx, "trend_start_price": self._trend_start_price,
            "candle_idx": self._candle_idx,
        }

    def restore_state(self, state):
        self._highs = state.get("highs", [])
        self._lows = state.get("lows", [])
        self._trend = state.get("trend", "")
        self._trend_line_price = state.get("trend_line_price")
        self._trend_line_slope = state.get("trend_line_slope", 0)
        self._trend_start_idx = state.get("trend_start_idx", 0)
        self._trend_start_price = state.get("trend_start_price", 0)
        self._candle_idx = state.get("candle_idx", 0)


class PivotAnchoredVP:
    """Pivot-Anchored Volume Profile: builds VP between Renko swing points."""

    def __init__(self, num_rows=89, value_area_pct=0.68):
        self.num_rows = num_rows
        self.value_area_pct = value_area_pct
        self._current_high = None
        self._current_low = None
        self._current_volumes = {}
        self._total_volume = 0
        self.poc = None
        self.vah = None
        self.val = None
        self._profiles = []

    def reset_profile(self, price):
        if self._current_volumes and self._current_high and self._current_low:
            self._calc_levels()
            self._profiles.append({
                "poc": self.poc, "vah": self.vah, "val": self.val,
                "high": self._current_high, "low": self._current_low,
            })
            if len(self._profiles) > 10:
                self._profiles = self._profiles[-10:]
        self._current_high = price
        self._current_low = price
        self._current_volumes = {}
        self._total_volume = 0

    def feed_price(self, price, volume=1):
        if self._current_high is None:
            self._current_high = price
            self._current_low = price
        self._current_high = max(self._current_high, price)
        self._current_low = min(self._current_low, price)
        row = round(price * 4) / 4
        self._current_volumes[row] = self._current_volumes.get(row, 0) + volume
        self._total_volume += volume

    def _calc_levels(self):
        if not self._current_volumes or self._total_volume == 0:
            return
        self.poc = max(self._current_volumes, key=self._current_volumes.get)
        target_vol = self._total_volume * self.value_area_pct
        sorted_prices = sorted(self._current_volumes.keys())
        poc_idx = sorted_prices.index(self.poc) if self.poc in sorted_prices else len(sorted_prices) // 2
        accumulated = self._current_volumes.get(self.poc, 0)
        lo_idx = poc_idx
        hi_idx = poc_idx
        while accumulated < target_vol and (lo_idx > 0 or hi_idx < len(sorted_prices) - 1):
            lo_vol = self._current_volumes.get(sorted_prices[lo_idx - 1], 0) if lo_idx > 0 else 0
            hi_vol = self._current_volumes.get(sorted_prices[hi_idx + 1], 0) if hi_idx < len(sorted_prices) - 1 else 0
            if lo_vol >= hi_vol and lo_idx > 0:
                lo_idx -= 1
                accumulated += lo_vol
            elif hi_idx < len(sorted_prices) - 1:
                hi_idx += 1
                accumulated += hi_vol
            else:
                lo_idx -= 1
                accumulated += lo_vol
        self.val = sorted_prices[lo_idx]
        self.vah = sorted_prices[hi_idx]

    def get_levels(self):
        self._calc_levels()
        return {"poc": self.poc, "vah": self.vah, "val": self.val}

    def save_state(self):
        return {
            "current_high": self._current_high, "current_low": self._current_low,
            "current_volumes": {str(k): v for k, v in self._current_volumes.items()},
            "total_volume": self._total_volume,
            "poc": self.poc, "vah": self.vah, "val": self.val,
            "profiles": self._profiles[-5:],
        }

    def restore_state(self, state):
        self._current_high = state.get("current_high")
        self._current_low = state.get("current_low")
        cv = state.get("current_volumes", {})
        self._current_volumes = {float(k): v for k, v in cv.items()}
        self._total_volume = state.get("total_volume", 0)
        self.poc = state.get("poc")
        self.vah = state.get("vah")
        self.val = state.get("val")
        self._profiles = state.get("profiles", [])


class SignalEngine:
    """Processes ticks and produces trading signals. One instance shared across all accounts."""

    def __init__(self, symbol, data_dir):
        self.symbol = symbol
        self.pv = POINT_VALUES.get(symbol, 20.0)
        self.renko = RenkoBrickBuilder(BRICK_SIZE)
        self.candles = CandleBuilder(CANDLE_SECONDS)
        self.candles_10s = CandleBuilder(10)
        self.candles_15s = CandleBuilder(15)
        self._bb_closes_10s = []
        self._bb_closes_15s = []
        self.bb_candles = CandleBuilder(MSS_CANDLE_SECONDS)
        self.candles_5m = CandleBuilder(300)
        self.trendline_5m = TrendLineDetector()
        self._5m_highs = []
        self._5m_lows = []
        self._5m_mss_bull = None
        self._5m_mss_bear = None
        self._5m_is_ranging = False
        self._5m_range_high = 0.0
        self._5m_range_low = 0.0
        self._filter_candle_builder = CandleBuilder(180)
        self.candles_15m = CandleBuilder(900)
        self._15m_candle_closes = []
        self._15m_ema_length = 5
        self.pavp = PivotAnchoredVP(num_rows=89, value_area_pct=0.68)
        self.trendline = TrendLineDetector()
        self.mss = MSSDetector()
        self.last_price = 0.0
        self.last_tick_time = 0.0
        self._prev_brick_dir = None
        self._pending_mss = None
        self._mss_brick_count = 0
        self._restart_ts = time.time()
        self._new_bricks_since_restart = 0
        self._restart_cooldown_done = False

        self._mss_bull_level = None   # swing high level (support for LONG)
        self._mss_bear_level = None   # swing low level (resistance for SHORT)
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

        self._vp_candle_bins = []  # list of {price_bin: tpo_count} per candle
        self._vp_totals = {}      # aggregate {price_bin: total_tpos}

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

    def get_bb_for_timeframe(self, secs):
        """Get BB(20,2) for a specific candle timeframe."""
        if secs == 10:
            closes = self._bb_closes_10s
        elif secs == 15:
            closes = self._bb_closes_15s
        else:
            closes = self._bb_candle_closes
        if len(closes) < BB_LENGTH:
            return None
        recent = closes[-BB_LENGTH:]
        middle = sum(recent) / BB_LENGTH
        variance = sum((c - middle) ** 2 for c in recent) / BB_LENGTH
        std = variance ** 0.5
        return middle + BB_MULT * std, middle, middle - BB_MULT * std

    def _calc_bb_reversal(self):
        """BB(20, 1.5) on 30s candles for reversal mode."""
        if len(self._bb_candle_closes) < BB_LENGTH:
            return None
        recent = self._bb_candle_closes[-BB_LENGTH:]
        middle = sum(recent) / BB_LENGTH
        variance = sum((c - middle) ** 2 for c in recent) / BB_LENGTH
        std = variance ** 0.5
        upper = middle + 1.5 * std
        lower = middle - 1.5 * std
        return upper, middle, lower

    def check_bb_reversal_setup(self, price):
        """Check if a full candle closed beyond BB(1.5). Returns 'SHORT_SETUP', 'LONG_SETUP', or None."""
        bb = self._calc_bb_reversal()
        if bb is None:
            return None
        upper, middle, lower = bb
        candles = self.candles.candles
        if len(candles) < 2:
            return None
        last = candles[-1]
        if last["close"] > upper and last["open"] > upper:
            return {"type": "SHORT_SETUP", "candle_high": last["high"], "bb_mid": middle, "bb_lower": lower, "bb_upper": upper}
        elif last["close"] < lower and last["open"] < lower:
            return {"type": "LONG_SETUP", "candle_low": last["low"], "bb_mid": middle, "bb_upper": upper, "bb_lower": lower}
        return None

    def _hull_at_offset(self, offset=0):
        """Compute Hull MA direction at given candle offset from end. Returns 'BUY', 'SELL', or None."""
        n = HULL_LENGTH
        sqrt_n = int(n ** 0.5)
        needed = n + sqrt_n + 1 + offset
        closes = self._bb_candle_closes
        if len(closes) < needed:
            return None
        if offset > 0:
            closes = closes[:-offset]
        def wma(data, period):
            if len(data) < period:
                return None
            s = data[-period:]
            w_sum = sum(s[i] * (i + 1) for i in range(period))
            denom = period * (period + 1) / 2
            return w_sum / denom
        half_n = max(1, n // 2)
        hull_series = []
        for i in range(sqrt_n + 1):
            end = len(closes) - (sqrt_n - i)
            sl = closes[:end]
            w1 = wma(sl, half_n)
            w2 = wma(sl, n)
            if w1 is None or w2 is None:
                return None
            hull_series.append(2 * w1 - w2)
        if len(hull_series) < sqrt_n:
            return None
        hma_now = wma(hull_series, sqrt_n)
        hma_prev = wma(hull_series[:-1], sqrt_n)
        if hma_now is None or hma_prev is None:
            return None
        if hma_now > hma_prev:
            return "BUY"
        elif hma_now < hma_prev:
            return "SELL"
        return None

    def hull_ma_signal(self):
        """Returns (signal, is_fresh) or None. is_fresh=True if Hull flipped within last HULL_FRESH_CANDLES."""
        current = self._hull_at_offset(0)
        if current is None:
            return None
        older = self._hull_at_offset(HULL_FRESH_CANDLES)
        is_fresh = older is not None and older != current
        return current, is_fresh

    def channel_state(self, lookback=120, slope_threshold=0.02):
        """Linear regression channel on recent 30s candle closes.
        Returns (slope_per_candle, upper_dev, lower_dev, last_regression_value) or None."""
        closes = self._bb_candle_closes
        n = len(closes)
        if n < lookback:
            return None
        data = closes[-lookback:]
        x_mean = (lookback - 1) / 2.0
        y_mean = sum(data) / lookback
        num = sum((i - x_mean) * (data[i] - y_mean) for i in range(lookback))
        den = sum((i - x_mean) ** 2 for i in range(lookback))
        if den == 0:
            return None
        slope = num / den
        intercept = y_mean - slope * x_mean
        residuals = [data[i] - (slope * i + intercept) for i in range(lookback)]
        std_res = (sum(r * r for r in residuals) / lookback) ** 0.5
        reg_end = slope * (lookback - 1) + intercept
        upper = reg_end + 1.5 * std_res
        lower = reg_end - 1.5 * std_res
        return slope, upper, lower, reg_end

    def _vp_add_candle(self, candle):
        low_bin = int(candle["low"] / VP_BIN_SIZE) * VP_BIN_SIZE
        high_bin = int(candle["high"] / VP_BIN_SIZE) * VP_BIN_SIZE
        bins = {}
        b = low_bin
        while b <= high_bin + 0.01:
            bins[b] = 1
            self._vp_totals[b] = self._vp_totals.get(b, 0) + 1
            b += VP_BIN_SIZE
        self._vp_candle_bins.append(bins)
        while len(self._vp_candle_bins) > VP_LOOKBACK_CANDLES:
            old = self._vp_candle_bins.pop(0)
            for ob, ov in old.items():
                self._vp_totals[ob] = self._vp_totals.get(ob, 0) - ov
                if self._vp_totals[ob] <= 0:
                    self._vp_totals.pop(ob, None)

    def volume_profile_check(self, price):
        if len(self._vp_candle_bins) < 60:
            return True, 0, 0.5, 0
        price_bin = round(price / VP_BIN_SIZE) * VP_BIN_SIZE
        vol = self._vp_totals.get(price_bin, 0)
        all_vols = sorted(self._vp_totals.values())
        rank = sum(1 for v in all_vols if v <= vol)
        pct = rank / len(all_vols)
        poc_bin = max(self._vp_totals, key=self._vp_totals.get)
        return pct >= VP_HVN_PERCENTILE, vol, pct, poc_bin

    def find_nearest_hvn(self, price, direction, nth=1):
        if len(self._vp_candle_bins) < 60 or not self._vp_totals:
            return None
        all_vols = sorted(self._vp_totals.values())
        threshold_idx = int(len(all_vols) * VP_HVN_PERCENTILE)
        min_vol = all_vols[threshold_idx] if threshold_idx < len(all_vols) else all_vols[-1]
        hvn_bins = [b for b, v in self._vp_totals.items() if v >= min_vol]
        if not hvn_bins:
            return None
        if direction == "LONG":
            below = sorted([b for b in hvn_bins if b < price - VP_BIN_SIZE], reverse=True)
            return below[nth - 1] if len(below) >= nth else (below[-1] if below else None)
        else:
            above = sorted([b for b in hvn_bins if b > price + VP_BIN_SIZE])
            return above[nth - 1] if len(above) >= nth else (above[-1] if above else None)

    def _bb_slope(self):
        """BB middle movement over last BB_SLOPE_LOOKBACK candles. Returns abs change or None."""
        n = len(self._bb_candle_closes)
        need = BB_LENGTH + BB_SLOPE_LOOKBACK
        if n < need:
            return None
        cur = self._bb_candle_closes[-BB_LENGTH:]
        old = self._bb_candle_closes[-(BB_LENGTH + BB_SLOPE_LOOKBACK):-BB_SLOPE_LOOKBACK]
        mid_now = sum(cur) / BB_LENGTH
        mid_old = sum(old) / BB_LENGTH
        return mid_now - mid_old

    def get_filter_candle_color(self):
        """Real-time forming 3m candle color (open vs last price).
        Boundaries align to ET clock (:00/:03/:06...) like TradingView."""
        fb = self._filter_candle_builder
        candle = fb._current
        if candle is None and fb.candles:
            candle = fb.candles[-1]
        if candle is None:
            return None
        o, c = candle["open"], candle["close"]
        if c > o:
            return "green"
        if c < o:
            return "red"
        return None

    def filter_candle_agrees(self, direction):
        """True when forming 3m candle color matches signal direction."""
        color = self.get_filter_candle_color()
        if direction == "LONG":
            return color == "green"
        if direction == "SHORT":
            return color == "red"
        return False

    def get_5m_state(self):
        """Returns 5min market state: trend direction, ranging, MSS levels."""
        trend = self.trendline_5m.get_trend()
        return {
            "trend": trend,
            "ranging": self._5m_is_ranging,
            "mss_bull": self._5m_mss_bull,
            "mss_bear": self._5m_mss_bear,
            "range_high": self._5m_range_high,
            "range_low": self._5m_range_low,
        }

    def check_5m_mss_invalidation(self, position, price):
        """Check if 5min MSS has shifted against our position. Returns True if invalidated."""
        if position == 1 and self._5m_mss_bear and price < self._5m_mss_bear:
            return True
        if position == -1 and self._5m_mss_bull and price > self._5m_mss_bull:
            return True
        return False

    def trend_15m(self):
        """Returns 'BULL', 'BEAR', or None based on 15m EMA trend."""
        closes = self._15m_candle_closes
        n = self._15m_ema_length
        if len(closes) < n + 1:
            return None
        def ema(data, period):
            k = 2 / (period + 1)
            val = data[0]
            for p in data[1:]:
                val = p * k + val * (1 - k)
            return val
        ema_now = ema(closes[-n:], n)
        ema_prev = ema(closes[-(n+1):-1], n)
        current_price = closes[-1]
        if current_price > ema_now and ema_now > ema_prev:
            return "BULL"
        elif current_price < ema_now and ema_now < ema_prev:
            return "BEAR"
        return None

    def extract_features(self):
        return self.ml.extract_features(price=self.last_price, renko=self.renko)

    def tick(self, price, ts=None):
        """Process tick and return list of signal actions.
        Flow: Queue both MSS levels -> BB confirms whichever side first -> enter
        - SHORT: bear MSS level (resistance) exists, BB middle below it, price at upper BB
        - LONG: bull MSS level (support) exists, BB middle above it, price at lower BB
        """
        if ts is None:
            ts = time.time()
        self.last_price = price
        self.pavp.feed_price(price)
        self.last_tick_time = ts
        signals = []

        # Feed 10s and 15s candles for per-account BB timeframes
        c10 = self.candles_10s.feed(price, ts)
        if c10:
            self._bb_closes_10s.append(c10["close"])
            if len(self._bb_closes_10s) > 500:
                self._bb_closes_10s = self._bb_closes_10s[-500:]
        c15 = self.candles_15s.feed(price, ts)
        if c15:
            self._bb_closes_15s.append(c15["close"])
            if len(self._bb_closes_15s) > 500:
                self._bb_closes_15s = self._bb_closes_15s[-500:]

        # Feed 30s candles for BB calculation + volume profile + trendline
        completed_candle = self.candles.feed(price, ts)
        if completed_candle:
            self._bb_candle_closes.append(completed_candle["close"])
            self._vp_add_candle(completed_candle)
            self.trendline.feed_candle(completed_candle)
            if len(self._bb_candle_closes) > self._max_bb_candle_history:
                self._bb_candle_closes = self._bb_candle_closes[-self._max_bb_candle_history:]

        # Feed 3m candle for entry color filter
        self._filter_candle_builder.feed(price, ts)

        # Feed 5m candles for higher TF trend filter + MSS + range detection
        completed_5m = self.candles_5m.feed(price, ts)
        if completed_5m:
            self.trendline_5m.feed_candle(completed_5m)
            self._5m_highs.append(completed_5m["high"])
            self._5m_lows.append(completed_5m["low"])
            if len(self._5m_highs) > 30:
                self._5m_highs = self._5m_highs[-30:]
                self._5m_lows = self._5m_lows[-30:]
            if len(self._5m_highs) >= 3:
                if self._5m_highs[-1] > self._5m_highs[-2] and self._5m_lows[-1] > self._5m_lows[-2]:
                    self._5m_mss_bull = self._5m_lows[-2]
                if self._5m_highs[-1] < self._5m_highs[-2] and self._5m_lows[-1] < self._5m_lows[-2]:
                    self._5m_mss_bear = self._5m_highs[-2]
            if len(self._5m_highs) >= 10:
                recent_h = self._5m_highs[-10:]
                recent_l = self._5m_lows[-10:]
                h_range = max(recent_h) - min(recent_h)
                l_range = max(recent_l) - min(recent_l)
                total_range = max(recent_h) - min(recent_l)
                self._5m_is_ranging = h_range < total_range * 0.4 and l_range < total_range * 0.4
                if self._5m_is_ranging:
                    self._5m_range_high = max(recent_h)
                    self._5m_range_low = min(recent_l)

        # Feed 15m candles for higher TF trend
        completed_15m = self.candles_15m.feed(price, ts)
        if completed_15m:
            self._15m_candle_closes.append(completed_15m["close"])
            if len(self._15m_candle_closes) > 50:
                self._15m_candle_closes = self._15m_candle_closes[-50:]

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
                slope = self._bb_slope()
                slope_str = f" s={slope:+.1f}" if slope is not None else ""
                lvl_str = ""
                if self._mss_bull_level:
                    lvl_str += f" | BULL@{self._mss_bull_level:.1f}"
                if self._mss_bear_level:
                    lvl_str += f" | BEAR@{self._mss_bear_level:.1f}"
                print(f"[{now_str}] [{self.symbol} BRICK] {brick['direction'].upper()} "
                      f"{brick['open']:.2f} -> {brick['close']:.2f} "
                      f"(consecutive: {self.renko.consecutive_count()}) | {mfi_str} | {bb_str}{slope_str}{lvl_str}")

                self._mss_brick_count += 1
                self._prev_brick_dir = brick["direction"]

        # MSS detection — queue both bull and bear levels simultaneously
        self.mss.update_swings(self.renko.bricks)
        if in_trade_session() and self._restart_cooldown_done:
            mss_signal = self.mss.check(price)
            if mss_signal:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                if mss_signal == "bearish" and self.mss.swing_lows:
                    new_level = self.mss.swing_lows[-1]
                    old = f" (was {self._mss_bear_level:.2f})" if self._mss_bear_level else ""
                    self._mss_bear_level = new_level
                    self.pavp.reset_profile(price)
                    pavp_levels = self.pavp.get_levels()
                    pavp_str = f" | PAVP poc={pavp_levels['poc']:.2f}" if pavp_levels['poc'] else ""
                    print(f"[{now_str}] [{self.symbol} MSS-BEAR] RESISTANCE @ {new_level:.2f}{old} "
                          f"— queued for SHORT when BB middle below + upper band touch{pavp_str}")
                elif mss_signal == "bullish" and self.mss.swing_highs:
                    new_level = self.mss.swing_highs[-1]
                    old = f" (was {self._mss_bull_level:.2f})" if self._mss_bull_level else ""
                    self._mss_bull_level = new_level
                    self.pavp.reset_profile(price)
                    pavp_levels = self.pavp.get_levels()
                    pavp_str = f" | PAVP poc={pavp_levels['poc']:.2f}" if pavp_levels['poc'] else ""
                    print(f"[{now_str}] [{self.symbol} MSS-BULL] SUPPORT @ {new_level:.2f}{old} "
                          f"— queued for LONG when BB middle above + lower band touch{pavp_str}")
                self.mss.reset()

        # Check both MSS levels against BB on every tick
        if in_trade_session() and self._restart_cooldown_done:
            bb = self._calc_bb()
            if bb and (self._mss_bear_level or self._mss_bull_level):
                upper, middle, lower = bb
                bb_slope = self._bb_slope()
                slope_flat = True  # BB slope filter removed

                bb_range = upper - lower
                # SHORT: bear MSS level position in BB band
                if self._mss_bear_level and bb_range > 0 and price >= upper:
                    mss_bb_pct = (self._mss_bear_level - lower) / bb_range
                    if mss_bb_pct >= 0.02:
                        if slope_flat:
                            features = self.extract_features()
                            should_enter, reason = self.ml.should_enter(features)
                            now_str = datetime.now(ET).strftime("%H:%M:%S")
                            slope_str = f"{bb_slope:+.1f}" if bb_slope is not None else "?"
                            print(f"[{now_str}] [{self.symbol} BB-CONFIRM] SHORT — "
                                  f"price {price:.2f} >= upper BB {upper:.2f} | "
                                  f"MSS resist {self._mss_bear_level:.2f} @ BB {mss_bb_pct:.0%} | "
                                  f"slope {slope_str} | {reason}")
                            if should_enter:
                                rl_idx, rl_params = self.rl.choose(features)
                                mss_level = self._mss_bear_level
                                signals.append(("enter_short", price, features, rl_idx, rl_params, mss_bb_pct, mss_level))
                                self._mss_bear_level = None
                                self._mss_bull_level = None
                        else:
                            now_str = datetime.now(ET).strftime("%H:%M:%S")
                            print(f"[{now_str}] [{self.symbol} BB-SLOPE-WAIT] SHORT — "
                                  f"at upper BB + MSS confirmed but slope steep ({bb_slope:+.1f})")

                # LONG: bull MSS level position in BB band
                if self._mss_bull_level and bb_range > 0 and price <= lower:
                    mss_bb_pct = (self._mss_bull_level - lower) / bb_range
                    if mss_bb_pct <= 0.98:
                        if slope_flat:
                            features = self.extract_features()
                            should_enter, reason = self.ml.should_enter(features)
                            now_str = datetime.now(ET).strftime("%H:%M:%S")
                            slope_str = f"{bb_slope:+.1f}" if bb_slope is not None else "?"
                            print(f"[{now_str}] [{self.symbol} BB-CONFIRM] LONG — "
                                  f"price {price:.2f} <= lower BB {lower:.2f} | "
                                  f"MSS support {self._mss_bull_level:.2f} @ BB {mss_bb_pct:.0%} | "
                                  f"slope {slope_str} | {reason}")
                            if should_enter:
                                rl_idx, rl_params = self.rl.choose(features)
                                mss_level = self._mss_bull_level
                                signals.append(("enter_long", price, features, rl_idx, rl_params, mss_bb_pct, mss_level))
                                self._mss_bear_level = None
                                self._mss_bull_level = None
                        else:
                            now_str = datetime.now(ET).strftime("%H:%M:%S")
                            print(f"[{now_str}] [{self.symbol} BB-SLOPE-WAIT] LONG — "
                                  f"at lower BB + MSS confirmed but slope steep ({bb_slope:+.1f})")

        return signals

    def save_state(self):
        return {
            "symbol": self.symbol, "saved_at": time.time(),
            "renko_last_close": self.renko._last_close,
            "renko_last_dir": self.renko._last_direction,
            "renko_bricks": self.renko.bricks[-50:],
            "prev_brick_dir": self._prev_brick_dir,
            "pending_mss": self._pending_mss,
            "mss_bull_level": self._mss_bull_level,
            "mss_bear_level": self._mss_bear_level,
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
            "vp_candle_bins": self._vp_candle_bins[-VP_LOOKBACK_CANDLES:],
            "15m_candle_closes": self._15m_candle_closes[-50:],
            "pavp": self.pavp.save_state(),
            "trendline": self.trendline.save_state(),
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
        self._mss_bull_level = state.get("mss_bull_level")
        self._mss_bear_level = state.get("mss_bear_level")
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
        saved_vp = state.get("vp_candle_bins", [])
        if saved_vp:
            self._vp_candle_bins = []
            self._vp_totals = {}
            for cb in saved_vp:
                converted = {float(k): v for k, v in cb.items()}
                self._vp_candle_bins.append(converted)
                for b, v in converted.items():
                    self._vp_totals[b] = self._vp_totals.get(b, 0) + v
        c15 = state.get("15m_candle_closes", [])
        if c15:
            self._15m_candle_closes = c15
        pavp_state = state.get("pavp")
        if pavp_state:
            self.pavp.restore_state(pavp_state)
        tl_state = state.get("trendline")
        if tl_state:
            self.trendline.restore_state(tl_state)
        bull_str = f"{self._mss_bull_level:.1f}" if self._mss_bull_level else "none"
        bear_str = f"{self._mss_bear_level:.1f}" if self._mss_bear_level else "none"
        vp_str = f", VP candles={len(self._vp_candle_bins)}" if self._vp_candle_bins else ""
        c15_str = f", 15m candles={len(self._15m_candle_closes)}" if self._15m_candle_closes else ""
        print(f"  [{self.symbol}] Signal engine restored: {len(self.renko.bricks)} bricks, "
              f"BB candles={len(self._bb_candle_closes)}, "
              f"MSS bull={bull_str} bear={bear_str}{vp_str}{c15_str}")


# ============================================================
# Account Connection — one per TopstepX account
# ============================================================

class AccountConnection:
    """Manages one TopstepX account: connection, position, PnL."""

    def __init__(self, name, username, api_key, account_name, symbol, base_qty,
                 tg_token="", tg_chat="", tg_keys=None, ntfy_topic="", tp_webhooks=None,
                 bb_threshold=0.6, proxy=None, strategy="classic", channel_filter=False,
                 pullback_pts=0, volume_profile=False, vp_defer=False, hull_filter=False, dca_hvn_nth=1):
        self.name = name
        self.username = username
        self.api_key = api_key
        self.account_name = account_name
        self.symbol = symbol
        self.base_qty = base_qty
        self.bb_threshold = bb_threshold
        self.proxy = proxy
        self.strategy = strategy
        self.channel_filter = channel_filter
        self.pullback_pts = pullback_pts
        self.volume_profile = volume_profile
        self.vp_defer = vp_defer
        self.hull_filter = hull_filter
        self.dca_hvn_nth = dca_hvn_nth
        self.hvn_entry = False
        self.hvn_offset = 15
        self.pv = POINT_VALUES.get(symbol, 20.0)
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.ntfy_topic = ntfy_topic
        self.tp_webhooks = tp_webhooks or []

        self.reverse_mode = False
        self.reverse_tp_pts = 25
        self.reverse_sl_pts = 30
        self.reverse_entry_pts = 5
        self._reverse_sl_price = 0.0
        self._reverse_tp_price = 0.0
        self.no_15m_filter = False
        self.pavp_mode = False
        self.trendline_mode = False
        self.bb_reversal_mode = False
        self._bb_rev_setup = None
        self._entry_trend = ""
        self._last_known_trend = ""
        self.hvn_trail_mode = False
        self._hvn_tp_levels = []
        self._hvn_tp_current_idx = 0
        self.fixed_tp_dollars = 0
        self.bb_candle_secs = CANDLE_SECONDS
        self._candle_color_check = None
        self._intended_entry_price = 0.0
        self._entry_spread = 0.0
        self._entry_slippage = 0.0
        self._trade_log_file = None
        self.rl_tp_enabled = False
        self._rl_tp = None
        self._rl_tp_action_idx = None
        self._rl_tp_params = None

        self.suite = None
        self.ctx = None

        self.position = 0
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_features = None
        self._entry_prices = []
        self._dca_done = False
        self._dca1_hvn_level = None
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

        self._flip_cumulative_pnl = 0.0
        self._flip_mss_level = None
        self._flip_count = 0
        self._flip_last_time = 0.0

        self._pending_pullback = None
        self._entering = False
        self._last_entry_attempt = 0.0

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
        self._last_reconnect = 0
        self._reconnect_failures = 0

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

        if self.proxy:
            os.environ["HTTP_PROXY"] = self.proxy
            os.environ["HTTPS_PROXY"] = self.proxy
            os.environ["ALL_PROXY"] = self.proxy
        else:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("ALL_PROXY", None)

        try:
            self.suite = await TradingSuite.create(
                instruments=symbols, timeframes=["1sec"], initial_days=1,
                features=["orderbook"])
        except Exception as e:
            err_msg = str(e)
            if "not found" in err_msg:
                import re
                all_accts = re.findall(r'[A-Z0-9]+-V2-(?:[A-Z]+-)?[\d]+-[\d]+', err_msg)
                all_accts = [a for a in set(all_accts) if a != self.account_name]
                prac = [a for a in all_accts if a.startswith("PRAC-")]
                express = [a for a in all_accts if a.startswith("EXPRESS-")]
                funded = [a for a in all_accts if any(a.startswith(p) for p in ("50KTC-", "150KTC-")) and "-DLL-" not in a]
                candidates = prac or express or funded or all_accts
                if candidates:
                    new_acct = candidates[0]
                    print(f"[{self.name} AUTO-SWITCH] {self.account_name} -> {new_acct}")
                    self.account_name = new_acct
                    os.environ["PROJECT_X_ACCOUNT_NAME"] = new_acct
                    self.suite = await TradingSuite.create(
                        instruments=symbols, timeframes=["1sec"], initial_days=1,
                        features=["orderbook"])
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
        proxy_info = f" | proxy: {self.proxy}" if self.proxy else ""
        strat_info = f" | strategy: {self.strategy}" if self.strategy != "classic" else ""
        ch_info = " | channel_filter: ON" if self.channel_filter else ""
        pb_info = f" | pullback: {self.pullback_pts}pts" if self.pullback_pts > 0 else ""
        vp_info = " | volume_profile: DEFER" if self.vp_defer else (" | volume_profile: ON" if self.volume_profile else "")
        hull_info = f" | hull({HULL_LENGTH}): ON" if self.hull_filter else ""
        hvn_entry_info = f" | entry: HVN+{self.hvn_offset}pt" if self.hvn_entry else ""
        dca_info = f" | DCA: {ordinal(self.dca_hvn_nth)} HVN+{self.hvn_offset}pt" if self.dca_hvn_nth > 1 or self.hvn_entry else ""
        rev_info = f" | REVERSE TP={self.reverse_tp_pts}pt SL={self.reverse_sl_pts}pt" if self.reverse_mode else ""
        print(f"[{self.name}] Connected: {self.account_name} | contract: {self.ctx.instrument_info.id} | BB threshold: {self.bb_threshold:.0%}{proxy_info}{strat_info}{ch_info}{pb_info}{vp_info}{hull_info}{hvn_entry_info}{dca_info}{rev_info}")

        if self.ctx.orderbook:
            print(f"[{self.name}] Level 2 orderbook: ACTIVE")
        else:
            print(f"[{self.name}] Level 2 orderbook: NOT AVAILABLE")

        await self._verify_position_on_connect()
        if self.position != 0 and self.entry_price > 0:
            await self._check_platform_tp_on_connect()
        self._needs_crash_recovery = True
        await self._sync_pnl_from_platform()

    async def record_l2_snapshot(self, event_type, price, l2_file):
        """Record Level 2 orderbook snapshot to file for future analysis."""
        if not self.ctx or not self.ctx.orderbook:
            return
        try:
            snapshot = await asyncio.wait_for(
                self.ctx.orderbook.get_orderbook_snapshot(levels=10), timeout=3.0)
            spread_data = await asyncio.wait_for(
                self.ctx.orderbook.get_bid_ask_spread(), timeout=3.0)
            imbalance = await asyncio.wait_for(
                self.ctx.orderbook.get_market_imbalance(levels=5), timeout=3.0)
            record = {
                "timestamp": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S.%f"),
                "account": self.name,
                "event": event_type,
                "price": round(price, 2),
                "best_bid": snapshot.get("best_bid"),
                "best_ask": snapshot.get("best_ask"),
                "spread": spread_data.get("spread") if isinstance(spread_data, dict) else spread_data,
                "bid_depth": snapshot.get("total_bid_volume", 0),
                "ask_depth": snapshot.get("total_ask_volume", 0),
                "imbalance_ratio": imbalance.get("imbalance_ratio", 0) if isinstance(imbalance, dict) else 0,
                "bids": snapshot.get("bids", [])[:5],
                "asks": snapshot.get("asks", [])[:5],
            }
            with open(l2_file, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"[{self.name} L2] Snapshot error: {e}")

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
                    if self._intended_entry_price > 0:
                        self._entry_slippage = abs(p.averagePrice - self._intended_entry_price)
                    if abs(old_avg - p.averagePrice) > 0.01:
                        slip_str = f" | slippage: {self._entry_slippage:.2f}pts" if self._entry_slippage > 0.01 else ""
                        print(f"[{self.name} PRICE-SYNC] {old_avg:.2f} -> {p.averagePrice:.2f}{slip_str}")
                    break
        except Exception:
            pass

    async def _adopt_phantom_position(self, expected_side, price, features, rl_idx, rl_params, qty):
        """After order timeout, check if platform actually filled the order."""
        try:
            await asyncio.sleep(2.0)
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    direction = "LONG" if expected_side == 1 else "SHORT"
                    self.position = expected_side
                    self.contracts_held = p.size
                    self.entry_price = p.averagePrice
                    self._entry_prices = [p.averagePrice]
                    self._dca_done = False
                    self._dca1_hvn_level = None
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
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.name} PHANTOM-ADOPT] {direction} x{p.size} "
                          f"@ {p.averagePrice:.2f} — order timed out but platform filled it")
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        direction, self.symbol, p.averagePrice, p.size),
                        kwargs={"ntfy_topic": self.ntfy_topic, "tp_webhooks": self.tp_webhooks},
                        daemon=True).start()
                    return True
            print(f"[{self.name} PHANTOM-CHECK] No position found — order truly failed")
            return False
        except Exception as e:
            print(f"[{self.name} PHANTOM-CHECK] Error: {e}")
            return False

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
                    old_held = self.contracts_held
                    self.contracts_held = p.size
                    self.entry_price = p.averagePrice
                    direction = "LONG" if self.position == 1 else "SHORT"
                    print(f"[{self.name} POS-VERIFY] {direction} x{p.size} confirmed @ {p.averagePrice:.2f}")
                    if self._dca_done and p.size <= 1:
                        print(f"[{self.name} POS-VERIFY] DCA was marked done but platform shows only {p.size} contract "
                              f"— resetting DCA so it can retry at next HVN zone")
                        self._dca_done = False
                        self._dca1_hvn_level = None
                    break
            if not found:
                old_dir = "LONG" if self.position == 1 else "SHORT"
                print(f"[{self.name} POS-VERIFY] {old_dir} x{self.contracts_held} NOT FOUND on platform — clearing phantom position")
                self.position = 0
                self.contracts_held = 0
                self.entry_price = 0.0
                self.entry_time = 0.0
                self._dca_done = False
                self._dca1_hvn_level = None
                self._entry_prices = []
                self._trail_profit_active = False
                self._trail_profit_peak = 0.0
                self._trail_profit_floor = 0.0
        except Exception as e:
            print(f"[{self.name} POS-VERIFY] Error: {e}")

    async def _check_platform_tp_on_connect(self):
        """Right after verify, check if platform shows position already at TP profit."""
        try:
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    platform_pnl = getattr(p, 'unrealizedPnl', None) or getattr(p, 'pnl', None)
                    direction = "LONG" if self.position == 1 else "SHORT"
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    if platform_pnl is not None and platform_pnl >= DCA_TP_DOLLARS:
                        print(f"[{now_str}] [{self.name} CONNECT-TP] {direction} x{p.size} — "
                              f"platform shows ${platform_pnl:.0f} profit >= ${DCA_TP_DOLLARS} — flattening NOW")
                        price_est = getattr(self, '_last_price', 0) or getattr(self, '_last_known_price', 0)
                        if price_est > 0:
                            await self.flatten(price_est, f"connect-TP ${platform_pnl:.0f}")
                        else:
                            print(f"[{now_str}] [{self.name} CONNECT-TP] No price yet — will close on first tick")
                    else:
                        pnl_str = f"${platform_pnl:.0f}" if platform_pnl is not None else "unknown"
                        print(f"[{now_str}] [{self.name} CONNECT-PNL] {direction} x{p.size} @ {self.entry_price:.2f} | "
                              f"platform P&L: {pnl_str}")
                    break
        except Exception as e:
            print(f"[{self.name} CONNECT-TP] Error checking platform P&L: {e}")

    async def _crash_recovery_check(self, price):
        """On first tick after restart, check if price already satisfies pending conditions."""
        self._needs_crash_recovery = False
        if not self.connected or price <= 0:
            return
        try:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            pv = self.pv

            if self.position != 0 and self.entry_price > 0:
                if self.position == 1:
                    pnl = (price - self.entry_price) * pv * self.contracts_held
                else:
                    pnl = (self.entry_price - price) * pv * self.contracts_held
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.name} CRASH-CHECK] {direction} x{self.contracts_held} | "
                      f"entry: {self.entry_price:.2f} | price: {price:.2f} | P&L: ${pnl:.0f}")
                if self.rl_tp_enabled and self._rl_tp_params:
                    rl_tp = self._rl_tp_params["tp_dollars"]
                    rl_sl = self._rl_tp_params["sl_dollars"]
                    if rl_tp > 0 and pnl >= rl_tp:
                        print(f"[{now_str}] [{self.name} CRASH-RL-TP] Already at ${pnl:.0f} >= TP ${rl_tp:.0f} — flattening NOW")
                        await self.flatten(price, f"crash-recovery RL-TP ${pnl:.0f}")
                        return
                    if rl_sl > 0 and pnl <= -rl_sl:
                        print(f"[{now_str}] [{self.name} CRASH-RL-SL] Already at ${pnl:.0f} <= SL -${rl_sl:.0f} — flattening NOW")
                        await self.flatten(price, f"crash-recovery RL-SL ${pnl:.0f}")
                        return
                    print(f"[{now_str}] [{self.name} CRASH-HOLD] P&L ${pnl:.0f} | RL: {self._rl_tp_params['label']} — holding")
                    return
                if self.fixed_tp_dollars > 0:
                    if pnl >= self.fixed_tp_dollars:
                        print(f"[{now_str}] [{self.name} CRASH-FIXED-TP] Already at ${pnl:.0f} profit — flattening NOW")
                        await self.flatten(price, f"crash-recovery FIXED-TP ${pnl:.0f}")
                        return
                    print(f"[{now_str}] [{self.name} CRASH-HOLD] Position P&L ${pnl:.0f}, target ${self.fixed_tp_dollars} — holding")
                    return
                elif self.reverse_mode and self._reverse_tp_price > 0:
                    tp_hit = (self.position == 1 and price >= self._reverse_tp_price) or \
                             (self.position == -1 and price <= self._reverse_tp_price)
                    if tp_hit:
                        print(f"[{now_str}] [{self.name} CRASH-REV-TP] Already at TP — flattening NOW")
                        await self.flatten(price, f"crash-recovery REV-TP ${pnl:.0f}")
                        return
                    print(f"[{now_str}] [{self.name} CRASH-HOLD] Position P&L ${pnl:.0f} — holding, no SL")
                    return
                if pnl >= DCA_TP_DOLLARS:
                    print(f"[{now_str}] [{self.name} CRASH-TP] ${pnl:.0f} >= ${DCA_TP_DOLLARS} — flattening NOW")
                    await self.flatten(price, f"crash-recovery TP ${pnl:.0f}")
                    return

            if self.position != 0 and not self._dca_done and self._dca1_hvn_level is not None:
                if self.contracts_held < DCA_MAX_CONTRACTS:
                    hit = (self.position == 1 and price <= self._dca1_hvn_level) or \
                          (self.position == -1 and price >= self._dca1_hvn_level)
                    if hit:
                        direction = "LONG" if self.position == 1 else "SHORT"
                        print(f"[{now_str}] [{self.name} CRASH-DCA] {direction} price {price:.2f} "
                              f"already past HVN zone {self._dca1_hvn_level:.0f} — DCA now")
                        await self.dca_add(price)

            if self._pending_pullback and self.position == 0:
                pb = self._pending_pullback
                sig_price = pb["signal_price"]
                ref_price = pb.get("original_signal", sig_price)
                if pb.get("hull_deferred"):
                    print(f"[{now_str}] [{self.name} CRASH-RECOVER] Restored Hull-deferred "
                          f"{pb['direction']} signal — waiting for Hull to confirm")
                else:
                    if pb["direction"] == "SHORT":
                        pullback_move = price - sig_price
                        profit_move = (ref_price - price) * pv
                    else:
                        pullback_move = sig_price - price
                        profit_move = (price - ref_price) * pv
                    if profit_move >= DCA_TP_DOLLARS:
                        print(f"[{now_str}] [{self.name} CRASH-SKIP] {pb['direction']} — "
                              f"price moved ${profit_move:.0f} profit while offline, skipping")
                        self._pending_pullback = None
                    elif pullback_move >= self.pullback_pts:
                        print(f"[{now_str}] [{self.name} CRASH-ENTRY] {pb['direction']} — "
                              f"pullback {pullback_move:.1f}pts already met, entering @ {price:.2f}")
                        if pb["direction"] == "LONG":
                            success = await self.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb.get("mss_level"))
                        else:
                            success = await self.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb.get("mss_level"))
                        if success:
                            self._pending_pullback = None
                    else:
                        print(f"[{now_str}] [{self.name} CRASH-RECOVER] Restored pending "
                              f"{pb['direction']} signal @ {sig_price:.2f} — still waiting for pullback")
        except Exception as e:
            print(f"[{self.name} CRASH-RECOVER] Error: {e}")

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

    def _log_trade(self, trade):
        if not self._trade_log_file:
            return
        try:
            existing = []
            if os.path.exists(self._trade_log_file):
                with open(self._trade_log_file) as f:
                    existing = json.load(f)
            existing.append(trade)
            with open(self._trade_log_file, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            print(f"[{self.name}] Trade log error: {e}")

    async def _platform_position(self):
        """Query platform for position state. Returns dict or None on error.
        {'size': int, 'avg_price': float, 'side': 1/-1} or {'size': 0} if flat."""
        try:
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    side = 1 if getattr(p, 'type', 0) == 0 else -1
                    return {"size": p.size, "avg_price": p.averagePrice, "side": side}
            return {"size": 0}
        except Exception:
            return None

    async def _platform_contract_count(self):
        """Query platform for actual contract count. Returns count or -1 on error."""
        pos = await self._platform_position()
        if pos is None:
            return -1
        return pos["size"]

    async def enter_long(self, price, features, rl_idx, rl_params, mss_level=None):
        if self.position != 0 or not self.connected:
            return False
        if self._candle_color_check:
            color = self._candle_color_check()
            if color != "green":
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.name} 3M-BLOCK] LONG blocked — 3m candle is {color or 'DOJI'}")
                return False
        if self._entering:
            return False
        if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
            print(f"[{self.name}] Daily limit hit — skipping entry")
            return False
        platform_count = await self._platform_contract_count()
        if platform_count != 0:
            if platform_count > 0:
                print(f"[{self.name}] HARD STOP: Platform shows {platform_count} contracts — blocking LONG entry")
                self.position = 1 if platform_count > 0 else self.position
                self.contracts_held = platform_count
            else:
                print(f"[{self.name}] HARD STOP: Can't reach TopstepX API — blocking entry until API responds")
            return False
        self._entering = True
        try:
            return await self._do_enter_long(price, features, rl_idx, rl_params, mss_level)
        finally:
            self._entering = False

    async def _do_enter_long(self, price, features, rl_idx, rl_params, mss_level=None):
        await self._ensure_flat()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")
        if self.strategy == "flip":
            self._flip_mss_level = mss_level
            self._flip_cumulative_pnl = 0.0
            self._flip_count = 0
            mss_str = f"{mss_level:.2f}" if mss_level else "?"
            print(f"[{now}] [{self.name}] >>> LONG x{qty} @ {price:.2f} | "
                  f"FLIP mode | MSS flip @ {mss_str} | Net TP=${FLIP_NET_TP} | P&L: ${self.live_pnl:.2f}")
        else:
            dca_str = "DCA at HVN zones"
            print(f"[{now}] [{self.name}] >>> LONG x{qty} @ {price:.2f} | "
                  f"TP=${DCA_TP_DOLLARS} | ({rl_params['label']}) | "
                  f"trail=${rl_params['trail_activate']:.0f}/{100*rl_params['trail_pullback']:.0f}% | "
                  f"{dca_str} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await asyncio.wait_for(asyncio.shield(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id, side=0, size=qty)),
                timeout=15.0)
            if response.success:
                self.position = 1
                self.contracts_held = qty
                self._intended_entry_price = price
                self.entry_price = price
                self._entry_prices = [price]
                self._dca_done = False if (not self.reverse_mode or self.fixed_tp_dollars > 0) else True
                self._dca1_hvn_level = None
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
                if self.rl_tp_enabled and self._rl_tp:
                    tp_idx, tp_params = self._rl_tp.choose(features or {})
                    self._rl_tp_action_idx = tp_idx
                    self._rl_tp_params = tp_params
                    print(f"[{now}] [{self.name} RL-TP] LONG — chose {tp_params['label']} "
                          f"(TP=${tp_params['tp_dollars']} SL=${tp_params['sl_dollars']}) "
                          f"eps={self._rl_tp.epsilon:.2f} trades={self._rl_tp.total_trades}")
                if self.reverse_mode:
                    self._reverse_tp_price = price + self.reverse_tp_pts
                    self._reverse_sl_price = price - self.reverse_sl_pts
                    print(f"[{now}] [{self.name} REV-LEVELS] LONG TP={self._reverse_tp_price:.2f} (+{self.reverse_tp_pts}pts) | SL={self._reverse_sl_price:.2f} (-{self.reverse_sl_pts}pts)")
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
            print(f"[{self.name}] Order TIMEOUT — checking platform for phantom fill...")
            adopted = await self._adopt_phantom_position(
                expected_side=1, price=price, features=features,
                rl_idx=rl_idx, rl_params=rl_params, qty=qty)
            return adopted
        except Exception as e:
            print(f"[{self.name}] Order ERROR: {e}")
            return False

    async def enter_short(self, price, features, rl_idx, rl_params, mss_level=None):
        if self.position != 0 or not self.connected:
            return False
        if self._candle_color_check:
            color = self._candle_color_check()
            if color != "red":
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.name} 3M-BLOCK] SHORT blocked — 3m candle is {color or 'DOJI'}")
                return False
        if self._entering:
            return False
        if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
            print(f"[{self.name}] Daily limit hit — skipping entry")
            return False
        platform_count = await self._platform_contract_count()
        if platform_count != 0:
            if platform_count > 0:
                print(f"[{self.name}] HARD STOP: Platform shows {platform_count} contracts — blocking SHORT entry")
                self.position = -1 if platform_count > 0 else self.position
                self.contracts_held = platform_count
            else:
                print(f"[{self.name}] HARD STOP: Can't reach TopstepX API — blocking entry until API responds")
            return False
        self._entering = True
        try:
            return await self._do_enter_short(price, features, rl_idx, rl_params, mss_level)
        finally:
            self._entering = False

    async def _do_enter_short(self, price, features, rl_idx, rl_params, mss_level=None):
        await self._ensure_flat()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")
        if self.strategy == "flip":
            self._flip_mss_level = mss_level
            self._flip_cumulative_pnl = 0.0
            self._flip_count = 0
            mss_str = f"{mss_level:.2f}" if mss_level else "?"
            print(f"[{now}] [{self.name}] >>> SHORT x{qty} @ {price:.2f} | "
                  f"FLIP mode | MSS flip @ {mss_str} | Net TP=${FLIP_NET_TP} | P&L: ${self.live_pnl:.2f}")
        else:
            dca_str = "DCA at HVN zones"
            print(f"[{now}] [{self.name}] >>> SHORT x{qty} @ {price:.2f} | "
                  f"TP=${DCA_TP_DOLLARS} | ({rl_params['label']}) | "
                  f"trail=${rl_params['trail_activate']:.0f}/{100*rl_params['trail_pullback']:.0f}% | "
                  f"{dca_str} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await asyncio.wait_for(asyncio.shield(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id, side=1, size=qty)),
                timeout=15.0)
            if response.success:
                self.position = -1
                self.contracts_held = qty
                self._intended_entry_price = price
                self.entry_price = price
                self._entry_prices = [price]
                self._dca_done = False if (not self.reverse_mode or self.fixed_tp_dollars > 0) else True
                self._dca1_hvn_level = None
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
                if self.rl_tp_enabled and self._rl_tp:
                    tp_idx, tp_params = self._rl_tp.choose(features or {})
                    self._rl_tp_action_idx = tp_idx
                    self._rl_tp_params = tp_params
                    print(f"[{now}] [{self.name} RL-TP] SHORT — chose {tp_params['label']} "
                          f"(TP=${tp_params['tp_dollars']} SL=${tp_params['sl_dollars']}) "
                          f"eps={self._rl_tp.epsilon:.2f} trades={self._rl_tp.total_trades}")
                if self.reverse_mode:
                    self._reverse_tp_price = price - self.reverse_tp_pts
                    self._reverse_sl_price = price + self.reverse_sl_pts
                    print(f"[{now}] [{self.name} REV-LEVELS] SHORT TP={self._reverse_tp_price:.2f} (-{self.reverse_tp_pts}pts) | SL={self._reverse_sl_price:.2f} (+{self.reverse_sl_pts}pts)")
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
            print(f"[{self.name}] Order TIMEOUT — checking platform for phantom fill...")
            adopted = await self._adopt_phantom_position(
                expected_side=-1, price=price, features=features,
                rl_idx=rl_idx, rl_params=rl_params, qty=qty)
            return adopted
        except Exception as e:
            print(f"[{self.name}] Order ERROR: {e}")
            return False

    async def dca_add(self, price):
        if not self.connected:
            return False
        try:
            positions = await asyncio.wait_for(
                self.suite.client.search_open_positions(), timeout=8.0)
            contract_id = self.ctx.instrument_info.id
            for p in positions:
                if p.contractId == contract_id and p.size > 0:
                    self.contracts_held = p.size
                    self.entry_price = p.averagePrice
                    if p.size >= DCA_MAX_CONTRACTS:
                        print(f"[{self.name} DCA-HVN] Platform already shows {p.size} contracts (max {DCA_MAX_CONTRACTS}) — skipping DCA")
                        self._dca_done = True
                        return False
                    break
        except Exception as e:
            print(f"[{self.name} DCA-HVN] Can't verify platform position: {e} — aborting DCA for safety")
            return False
        qty = self.base_qty
        side = 0 if self.position == 1 else 1
        direction = "LONG" if self.position == 1 else "SHORT"
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [{self.name} DCA-HVN] {direction} x{qty} @ {price:.2f} | "
              f"Platform: {self.contracts_held} -> {self.contracts_held + qty} contracts")
        self._dca_done = True
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
                await self._sync_entry_price_from_platform()
                return True
            else:
                print(f"[{self.name} DCA-HVN] FAILED: {response}")
                self._dca_done = False
                return False
        except Exception as e:
            print(f"[{self.name} DCA-HVN] ERROR: {e} — checking platform for phantom fill")
            try:
                await asyncio.sleep(2.0)
                positions = await asyncio.wait_for(
                    self.suite.client.search_open_positions(), timeout=8.0)
                contract_id = self.ctx.instrument_info.id
                for p in positions:
                    if p.contractId == contract_id and p.size > 0:
                        if p.size > self.contracts_held:
                            print(f"[{self.name} DCA-HVN] Phantom fill confirmed — platform shows {p.size} contracts")
                            self.contracts_held = p.size
                            self.entry_price = p.averagePrice
                            self._dca_done = True
                            return True
                        break
                print(f"[{self.name} DCA-HVN] No phantom fill — resetting DCA to retry")
            except Exception:
                print(f"[{self.name} DCA-HVN] Can't verify platform — keeping DCA done to be safe")
                return False
            self._dca_done = False
            self._dca1_hvn_level = None
            return False

    async def flip_position(self, price, signal_engine=None):
        """Flip position at MSS level: close current, open opposite."""
        if self.position == 0 or not self.connected:
            return
        old_dir = "LONG" if self.position == 1 else "SHORT"
        new_dir = "SHORT" if self.position == 1 else "LONG"
        new_side = 1 if self.position == 1 else 0
        now_str = datetime.now(ET).strftime("%H:%M:%S")

        if self.position == 1:
            pts = price - self.entry_price
        else:
            pts = self.entry_price - price
        trade_pnl = pts * self.pv * self.contracts_held

        closed = False
        for attempt in range(3):
            try:
                await asyncio.wait_for(asyncio.shield(
                    self.ctx.positions.close_position_direct(
                        contract_id=self.ctx.instrument_info.id)),
                    timeout=15.0)
                closed = True
                break
            except Exception as e:
                print(f"[{self.name} FLIP] CLOSE ERROR (attempt {attempt+1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
        if not closed:
            print(f"[{self.name} FLIP] CLOSE FAILED after 3 attempts — staying in position")
            self._flip_last_time = time.time()
            return

        self._flip_cumulative_pnl += trade_pnl
        self.live_pnl += trade_pnl
        self.daily_loss += min(0, trade_pnl)
        self._flip_count += 1
        self._flip_last_time = time.time()

        print(f"[{now_str}] [{self.name} FLIP #{self._flip_count}] {old_dir} -> {new_dir} "
              f"@ {price:.2f} | This: ${trade_pnl:.2f} | Net: ${self._flip_cumulative_pnl:.2f}")

        await self._sync_pnl_from_platform()

        self.position = 0
        self.contracts_held = 0
        qty = self.base_qty

        try:
            response = await asyncio.wait_for(asyncio.shield(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id, side=new_side, size=qty)),
                timeout=15.0)
            if response.success:
                self.position = -1 if new_dir == "SHORT" else 1
                self.contracts_held = qty
                self.entry_price = price
                self._entry_prices = [price]
                self.entry_time = time.time()
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._trail_profit_active = False
                await self._sync_entry_price_from_platform()
                print(f"[{now_str}] [{self.name}] >>> {new_dir} x{qty} @ {price:.2f} | "
                      f"FLIP #{self._flip_count} | Net: ${self._flip_cumulative_pnl:.2f}")
                threading.Thread(target=send_telegram, args=(
                    self.tg_token, self.tg_chat,
                    f"FLIP #{self._flip_count} | {self.name}\n"
                    f"{old_dir} -> {new_dir} @ {price:.2f}\n"
                    f"This: ${trade_pnl:.2f} | Net: ${self._flip_cumulative_pnl:.2f}"),
                    daemon=True).start()
            else:
                print(f"[{self.name} FLIP] RE-ENTRY FAILED: {response}")
                self._flip_cumulative_pnl = 0.0
                self._flip_count = 0
                self._flip_mss_level = None
        except Exception as e:
            print(f"[{self.name} FLIP] RE-ENTRY ERROR: {e}")

    async def flatten(self, price, reason="signal", signal_engine=None):
        if self.position == 0 or not self.connected:
            return
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl_before = self.live_pnl

        if "TP" in reason or "PAVP-TP" in reason or "TL-TP" in reason or "REV-TP" in reason:
            pos = await self._platform_position()
            if pos and pos.get("size", 0) > 0:
                platform_entry = pos.get("avg_price", self.entry_price)
                if self.position == 1:
                    real_pnl = (price - platform_entry) * self.pv * pos["size"]
                else:
                    real_pnl = (platform_entry - price) * self.pv * pos["size"]
                if real_pnl <= 0:
                    print(f"[{now_str}] [{self.name} TP-VERIFY] Calculated TP but platform P&L is ${real_pnl:.0f} "
                          f"(entry {platform_entry:.2f}, price {price:.2f}) — NOT closing, waiting for real profit")
                    return
                print(f"[{now_str}] [{self.name} TP-VERIFY] Platform P&L: ${real_pnl:.0f} — closing")

        closed = False
        for attempt in range(1, 4):
            try:
                await asyncio.wait_for(asyncio.shield(
                    self.ctx.positions.close_position_direct(
                        contract_id=self.ctx.instrument_info.id)),
                    timeout=15.0)
                closed = True
                break
            except asyncio.TimeoutError:
                print(f"[{self.name}] CLOSE TIMEOUT (attempt {attempt}/3) — retrying...")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[{self.name}] CLOSE ERROR (attempt {attempt}/3): {e} — retrying...")
                await asyncio.sleep(1)
        if not closed:
            print(f"[{self.name}] CLOSE FAILED after 3 attempts — will retry on next tick")
            return

        if self.position == 1:
            pts = price - self.entry_price
        else:
            pts = self.entry_price - price
        trade_pnl = pts * self.pv * self.contracts_held
        self.live_pnl += trade_pnl
        self.daily_loss += min(0, trade_pnl)

        if self.strategy == "flip" and self._flip_count > 0:
            net_pnl = self._flip_cumulative_pnl + trade_pnl
            print(f"[{now_str}] [{self.name}] <<< EXIT {direction} x{self.contracts_held} "
                  f"@ {price:.2f} | Net: ${net_pnl:.2f} ({self._flip_count} flips) | "
                  f"Session: ${self.live_pnl:.2f} | {reason}")
        else:
            print(f"[{now_str}] [{self.name}] <<< EXIT {direction} x{self.contracts_held} "
                  f"@ {price:.2f} | Trade: ${trade_pnl:.2f} | Session: ${self.live_pnl:.2f} | {reason}")

        rl_idx = self._rl_action_idx
        rl_feat = self.entry_features
        mae = self.trade_mae
        mfe = self.trade_mfe

        self._log_trade({
            "account": self.name,
            "direction": direction,
            "intended_entry": round(self._intended_entry_price, 2),
            "actual_entry": round(self.entry_price, 2),
            "entry_slippage": round(self._entry_slippage, 2),
            "exit_price": price,
            "contracts": self.contracts_held,
            "pnl_calc": round(trade_pnl, 2),
            "mae": round(mae, 2),
            "mfe": round(mfe, 2),
            "reason": reason,
            "time_in_trade": round(time.time() - self.entry_time, 1),
            "reverse_mode": self.reverse_mode,
            "timestamp": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
        })

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

        if self.rl_tp_enabled and self._rl_tp and self._rl_tp_action_idx is not None and rl_feat:
            self._rl_tp.update(rl_feat, self._rl_tp_action_idx, trade_pnl)
            tp_label = self._rl_tp_params["label"] if self._rl_tp_params else "?"
            print(f"[{self.name} RL-TP] Updated Q-table: {tp_label} reward=${trade_pnl:.0f} "
                  f"eps={self._rl_tp.epsilon:.3f} trades={self._rl_tp.total_trades}")

        self.entry_features = None
        self._rl_action_idx = None
        self._rl_params = None
        self._rl_tp_action_idx = None
        self._rl_tp_params = None
        self._pending_rl = None
        self._flip_cumulative_pnl = 0.0
        self._flip_mss_level = None
        self._flip_count = 0
        self._flip_last_time = 0.0

    def check_position(self, price):
        """Check position-level exits: TP, trailing, DCA, or flip."""
        if self.position == 0:
            return []

        if self.position == 1:
            unrealized = (price - self.entry_price) * self.pv * self.contracts_held
        else:
            unrealized = (self.entry_price - price) * self.pv * self.contracts_held
        self.trade_mfe = max(self.trade_mfe, unrealized)
        self.trade_mae = min(self.trade_mae, unrealized)

        if self.rl_tp_enabled and self._rl_tp_params:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            rl_tp = self._rl_tp_params["tp_dollars"]
            rl_sl = self._rl_tp_params["sl_dollars"]
            if rl_tp > 0 and unrealized >= rl_tp:
                print(f"[{now_str}] [{self.name} RL-TP-HIT] {direction} x{self.contracts_held} "
                      f"${unrealized:.0f} >= TP ${rl_tp:.0f} ({self._rl_tp_params['label']}) | MFE: ${self.trade_mfe:.0f}")
                return [("flatten", price, "RL-TP")]
            if rl_sl > 0 and unrealized <= -rl_sl:
                print(f"[{now_str}] [{self.name} RL-SL-HIT] {direction} x{self.contracts_held} "
                      f"${unrealized:.0f} <= SL -${rl_sl:.0f} ({self._rl_tp_params['label']}) | MAE: ${self.trade_mae:.0f}")
                return [("flatten", price, "RL-SL")]
            return []

        if self.fixed_tp_dollars > 0:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            if unrealized >= self.fixed_tp_dollars:
                print(f"[{now_str}] [{self.name} FIXED-TP] {direction} x{self.contracts_held} "
                      f"${unrealized:.0f} >= ${self.fixed_tp_dollars:.0f} target | P&L: ${unrealized:.0f}")
                return [("flatten", price, "FIXED-TP")]
            # DCA disabled based on data analysis
            return []

        if (self.reverse_mode or self.pavp_mode or self.trendline_mode or self.bb_reversal_mode or self.hvn_trail_mode) and self._reverse_tp_price > 0:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            prefix = "HVN-TRAIL" if self.hvn_trail_mode else ("BB-REV" if self.bb_reversal_mode else ("TL" if self.trendline_mode else ("PAVP" if self.pavp_mode else "REV")))
            tp_hit = (self.position == 1 and price >= self._reverse_tp_price) or \
                     (self.position == -1 and price <= self._reverse_tp_price)
            if tp_hit:
                if self.hvn_trail_mode and self._hvn_tp_levels and self._hvn_tp_current_idx < len(self._hvn_tp_levels) - 1:
                    self._hvn_tp_current_idx += 1
                    next_tp = self._hvn_tp_levels[self._hvn_tp_current_idx]
                    self._reverse_sl_price = self._reverse_tp_price
                    self._reverse_tp_price = next_tp
                    print(f"[{now_str}] [{self.name} HVN-TRAIL-NEXT] {direction} — "
                          f"HVN {self._hvn_tp_current_idx} hit, trailing to next HVN {next_tp:.2f} | "
                          f"SL moved to {self._reverse_sl_price:.2f} | P&L: ${unrealized:.0f}")
                    return []
                print(f"[{now_str}] [{self.name} {prefix}-TP] {direction} x{self.contracts_held} "
                      f"price {price:.2f} hit TP {self._reverse_tp_price:.2f} | P&L: ${unrealized:.0f}")
                return [("flatten", price, f"{prefix}-TP")]
            if self.trendline_mode or self.bb_reversal_mode or self.hvn_trail_mode:
                if getattr(self, '_5m_mss_invalidated', False):
                    print(f"[{now_str}] [{self.name} 5M-MSS-SL] {direction} invalidated — "
                          f"5min external MSS shifted | P&L: ${unrealized:.0f}")
                    return [("flatten", price, "5M-MSS-SL")]
            sl_hit = (self.position == 1 and price <= self._reverse_sl_price) or \
                     (self.position == -1 and price >= self._reverse_sl_price)
            if sl_hit:
                print(f"[{now_str}] [{self.name} {prefix}-SL] {direction} x{self.contracts_held} "
                      f"price {price:.2f} hit max SL {self._reverse_sl_price:.2f} | P&L: ${unrealized:.0f}")
                return [("flatten", price, f"{prefix}-SL")]
            return []

        if self.strategy == "flip":
            net_pnl = self._flip_cumulative_pnl + unrealized
            if net_pnl >= FLIP_NET_TP:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.name} NET-TP] {direction} net ${net_pnl:.0f} >= "
                      f"${FLIP_NET_TP} ({self._flip_count} flips)")
                return [("flatten", price, "NET_TP")]
            if self._flip_mss_level is not None:
                cooldown_ok = (time.time() - self._flip_last_time) >= FLIP_COOLDOWN_SECS
                if not cooldown_ok:
                    return []
                if self.position == 1 and price <= (self._flip_mss_level - FLIP_BUFFER_PTS):
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.name} MSS-CROSS] LONG price {price:.2f} <= "
                          f"MSS {self._flip_mss_level:.2f} - {FLIP_BUFFER_PTS} — flipping to SHORT")
                    return [("flip", price)]
                if self.position == -1 and price >= (self._flip_mss_level + FLIP_BUFFER_PTS):
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.name} MSS-CROSS] SHORT price {price:.2f} >= "
                          f"MSS {self._flip_mss_level:.2f} + {FLIP_BUFFER_PTS} — flipping to LONG")
                    return [("flip", price)]
            return []

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
            if self._trail_profit_floor >= TP_SLIPPAGE_BUFFER:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.name} TRAIL-EXIT] ${unrealized:.0f} <= floor ${self._trail_profit_floor:.0f}")
                return [("flatten", price, "TRAIL_PROFIT")]
            else:
                self._trail_profit_active = False
                self._trail_profit_peak = 0.0
                self._trail_profit_floor = 0.0

        # TP (with slippage buffer so platform P&L is at target)
        tp_target = DCA_TP_DOLLARS + TP_SLIPPAGE_BUFFER
        if unrealized >= tp_target:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            print(f"[{now_str}] [{self.name} TP-HIT] {direction} x{self.contracts_held} "
                  f"${unrealized:.0f} >= ${tp_target:.0f} (target ${DCA_TP_DOLLARS:.0f} + ${TP_SLIPPAGE_BUFFER:.0f} buffer) — market exit")
            return [("flatten", price, "TP")]

        # DCA1 - first HVN zone (10pt past)
        if self._dca1_hvn_level is not None and not self._dca_done and self.contracts_held < DCA_MAX_CONTRACTS:
            if (self.position == 1 and price <= self._dca1_hvn_level) or \
               (self.position == -1 and price >= self._dca1_hvn_level):
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.name} DCA1-HVN-TRIGGER] {direction} — "
                      f"price {price:.2f} hit HVN+10 zone {self._dca1_hvn_level:.0f}")
                return [("dca_hvn1", price)]

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
            "dca1_hvn_level": self._dca1_hvn_level,
            "entry_prices": self._entry_prices,
            "rl_action_idx": self._rl_action_idx, "rl_params": self._rl_params,
            "rl_tp_action_idx": self._rl_tp_action_idx, "rl_tp_params": self._rl_tp_params,
            "tp_lock_pnl": self._tp_lock_pnl,
            "flip_cumulative_pnl": self._flip_cumulative_pnl,
            "flip_mss_level": self._flip_mss_level,
            "flip_count": self._flip_count,
            "pending_pullback": self._pending_pullback,
            "reverse_tp_price": self._reverse_tp_price,
            "reverse_sl_price": self._reverse_sl_price,
        }

    def restore_state(self, state, position_ttl=600):
        age = time.time() - state.get("saved_at", 0)
        self.daily_loss = state.get("daily_loss", 0.0)
        self._dca_done = state.get("dca_done", False)
        self._dca1_hvn_level = state.get("dca1_hvn_level")
        self._pending_pullback = state.get("pending_pullback")
        self._entry_prices = state.get("entry_prices", [])
        self._rl_action_idx = state.get("rl_action_idx")
        self._rl_params = state.get("rl_params")
        self._rl_tp_action_idx = state.get("rl_tp_action_idx")
        self._rl_tp_params = state.get("rl_tp_params")
        self._tp_lock_pnl = state.get("tp_lock_pnl", 0.0)
        self._reverse_tp_price = state.get("reverse_tp_price", 0.0)
        self._reverse_sl_price = state.get("reverse_sl_price", 0.0)

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
            self._flip_cumulative_pnl = state.get("flip_cumulative_pnl", 0.0)
            self._flip_mss_level = state.get("flip_mss_level")
            self._flip_count = state.get("flip_count", 0)
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
        self.state_file = os.path.join(data_dir, f"bot_state_{symbol}.json")

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
                bb_threshold=acfg.get("bb_threshold", 0.6),
                proxy=acfg.get("proxy"),
                strategy=acfg.get("strategy", "classic"),
                channel_filter=acfg.get("channel_filter", False),
                pullback_pts=acfg.get("pullback_pts", 0),
                volume_profile=acfg.get("volume_profile", False),
                vp_defer=acfg.get("vp_defer", False),
                hull_filter=acfg.get("hull_filter", False),
                dca_hvn_nth=acfg.get("dca_hvn_nth", 1),
            )
            acc.hvn_entry = acfg.get("hvn_entry", False)
            acc.hvn_offset = acfg.get("hvn_offset", 15)
            acc.reverse_mode = acfg.get("reverse_mode", False)
            acc.reverse_tp_pts = acfg.get("reverse_tp_pts", 25)
            acc.reverse_sl_pts = acfg.get("reverse_sl_pts", 30)
            acc.reverse_entry_pts = acfg.get("reverse_entry_pts", 5)
            acc.no_15m_filter = acfg.get("no_15m_filter", False)
            acc.pavp_mode = acfg.get("pavp_mode", False)
            acc.trendline_mode = acfg.get("trendline_mode", False)
            acc.bb_reversal_mode = acfg.get("bb_reversal_mode", False)
            acc.hvn_trail_mode = acfg.get("hvn_trail_mode", False)
            acc.fixed_tp_dollars = acfg.get("fixed_tp_dollars", 0)
            acc.bb_candle_secs = acfg.get("bb_candle_secs", CANDLE_SECONDS)
            acc.rl_tp_enabled = acfg.get("rl_tp_enabled", False)
            if acc.rl_tp_enabled:
                acc._rl_tp = TpSlRL(os.path.join(data_dir, f"rl_tp_state_{acfg['name']}_{symbol}.json"))
                print(f"[{acc.name}] RL TP/SL optimizer: ENABLED ({len(RL_TP_ACTIONS)} actions)")
            acc._trade_log_file = os.path.join(data_dir, f"trade_log_{acfg['name']}.json")
            acc._candle_color_check = lambda: self.engine.get_filter_candle_color() if hasattr(self, 'engine') else None
            acc.config_file = config_file
            self.accounts.append(acc)

        self.last_tick_time = time.time()
        self.last_real_tick_time = 0.0

        self._reconnecting = False
        self._reconnect_failures = 0
        self._reconnect_cooldown = RECONNECT_COOLDOWN_BASE
        self._last_reconnect_time = 0
        self._last_heartbeat = 0

        self._engine_source = any(acfg.get("engine_source", False) for acfg in accounts_config)
        self._engine_sync = any(acfg.get("engine_sync", False) for acfg in accounts_config)
        self._last_engine_upload = 0
        self._last_engine_download = 0
        self._s3_bucket = "topstepx-bot-sync"
        self._s3_key = f"engine_state_{symbol}.json"
        self._s3_trade_key = f"trade_signal_{symbol}.json"
        self._is_leader = False
        self._is_follower = False
        self._last_trade_signal_id = ""
        self._last_trade_upload = 0

        self.shadow_file = os.path.join(data_dir, "shadow_tracking.json")
        self._shadow_active = {}
        self._shadow_log = []
        self._load_shadow_log()
        self._l2_file = os.path.join(data_dir, f"l2_orderbook_{symbol}.jsonl")
        self._last_l2_snapshot = 0

    def save_all_state(self):
        try:
            shadow_active_serializable = {str(k): v for k, v in self._shadow_active.items()}
            state = {
                "engine": self.engine.save_state(),
                "accounts": {a.name: a.save_state() for a in self.accounts},
                "shadow_active": shadow_active_serializable,
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[STATE] Save error: {e}")

    def _upload_engine_to_s3(self):
        if not self._engine_source:
            return
        now = time.time()
        if now - self._last_engine_upload < 60:
            return
        self._last_engine_upload = now
        try:
            import boto3
            engine_state = self.engine.save_state()
            data = json.dumps(engine_state).encode()
            s3 = boto3.client("s3")
            s3.put_object(Bucket=self._s3_bucket, Key=self._s3_key, Body=data)
        except Exception as e:
            print(f"[ENGINE-SYNC] Upload error: {e}")

    def _download_engine_from_s3(self):
        if not self._engine_sync:
            return False
        try:
            import boto3
            s3 = boto3.client("s3")
            resp = s3.get_object(Bucket=self._s3_bucket, Key=self._s3_key)
            data = json.loads(resp["Body"].read())
            self.engine.restore_state(data)
            self._last_engine_download = time.time()
            print(f"[ENGINE-SYNC] Downloaded engine state from source bot")
            return True
        except Exception as e:
            print(f"[ENGINE-SYNC] Download error: {e}")
            return False

    def _sync_engine_from_s3(self):
        if not self._engine_sync:
            return
        now = time.time()
        if now - self._last_engine_download < 60:
            return
        try:
            import boto3
            s3 = boto3.client("s3")
            resp = s3.get_object(Bucket=self._s3_bucket, Key=self._s3_key)
            data = json.loads(resp["Body"].read())
            self.engine.restore_state(data)
            self._last_engine_download = now
        except Exception:
            pass

    def _broadcast_trade_signal(self, action, direction, price, features=None, rl_idx=None, rl_params=None, mss_level=None):
        if not self._is_leader:
            return
        try:
            import boto3
            signal = {
                "id": f"{time.time():.6f}",
                "action": action,
                "direction": direction,
                "price": price,
                "timestamp": time.time(),
                "features": features,
                "rl_idx": rl_idx,
                "rl_params": rl_params,
                "mss_level": mss_level,
            }
            data = json.dumps(signal).encode()
            s3 = boto3.client("s3")
            s3.put_object(Bucket=self._s3_bucket, Key=self._s3_trade_key, Body=data)
        except Exception as e:
            print(f"[LEADER] Trade signal broadcast error: {e}")

    async def _check_leader_signals(self, price):
        if not self._is_follower:
            return
        try:
            import boto3
            s3 = boto3.client("s3")
            resp = s3.get_object(Bucket=self._s3_bucket, Key=self._s3_trade_key)
            signal = json.loads(resp["Body"].read())
            sig_id = signal.get("id", "")
            if sig_id == self._last_trade_signal_id:
                return
            if time.time() - signal.get("timestamp", 0) > 30:
                self._last_trade_signal_id = sig_id
                return
            self._last_trade_signal_id = sig_id
            action = signal["action"]
            direction = signal["direction"]
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            for acc in self.accounts:
                if not acc.connected:
                    continue
                if action == "enter":
                    if acc.position != 0:
                        continue
                    print(f"[{now_str}] [{acc.name} COPY] {direction} entry from leader @ {price:.2f}")
                    features = signal.get("features") or {}
                    rl_idx = signal.get("rl_idx", 0)
                    rl_params = signal.get("rl_params") or {"label": "copy", "sl_pts": DEFAULT_SL_PTS, "trail_activate": TRAIL_PROFIT_ACTIVATE, "trail_pullback": TRAIL_PROFIT_PULLBACK}
                    mss_level = signal.get("mss_level")
                    if direction == "LONG":
                        await acc.enter_long(price, features, rl_idx, rl_params, mss_level=mss_level)
                    else:
                        await acc.enter_short(price, features, rl_idx, rl_params, mss_level=mss_level)
                elif action == "exit":
                    if acc.position == 0:
                        continue
                    acc_dir = "LONG" if acc.position == 1 else "SHORT"
                    if acc_dir == direction:
                        print(f"[{now_str}] [{acc.name} COPY] {direction} exit from leader")
                        await acc.flatten(price, "copy-leader-TP")
                elif action == "dca":
                    if acc.position == 0 or acc._dca_done:
                        continue
                    print(f"[{now_str}] [{acc.name} COPY] {direction} DCA from leader @ {price:.2f}")
                    await acc.dca_add(price)
        except Exception:
            pass

    def load_all_state(self):
        if not os.path.exists(self.state_file):
            old_state = os.path.join(self.data_dir, "bot_state_multi.json")
            if os.path.exists(old_state) and self.symbol == "NQ":
                self.state_file = old_state
            else:
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
            shadow_saved = state.get("shadow_active", {})
            self._shadow_active = {float(k): v for k, v in shadow_saved.items()}
            return True
        except Exception as e:
            print(f"[STATE] Load error: {e}")
            return False

    def _load_shadow_log(self):
        try:
            if os.path.exists(self.shadow_file):
                with open(self.shadow_file) as f:
                    self._shadow_log = json.load(f)
        except Exception:
            self._shadow_log = []

    def _save_shadow_log(self):
        try:
            tmp = self.shadow_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._shadow_log, f, indent=2)
            os.replace(tmp, self.shadow_file)
        except Exception as e:
            print(f"[SHADOW] Save error: {e}")

    def shadow_check_signal(self, direction, price, mss_bb_pct, mss_level):
        sym = self.symbol.split(":")[0]
        pv = POINT_VALUES.get(sym, 20.0)
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        for thresh in SHADOW_THRESHOLDS:
            if thresh in self._shadow_active:
                continue
            would_trigger = False
            if direction == "SHORT" and mss_bb_pct >= thresh:
                would_trigger = True
            elif direction == "LONG" and mss_bb_pct <= (1.0 - thresh):
                would_trigger = True
            if would_trigger:
                self._shadow_active[thresh] = {
                    "threshold": thresh,
                    "direction": direction,
                    "entry_price": price,
                    "mss_level": mss_level,
                    "mss_bb_pct": round(mss_bb_pct, 4),
                    "entry_time": now_str,
                    "mfe_dollars": 0.0,
                    "mae_dollars": 0.0,
                    "mfe_pts": 0.0,
                    "mae_pts": 0.0,
                }
                print(f"[SHADOW] {thresh:.0%} — {direction} @ {price:.2f} (BB {mss_bb_pct:.0%})")

    def shadow_tick(self, price):
        if not self._shadow_active:
            return
        sym = self.symbol.split(":")[0]
        pv = POINT_VALUES.get(sym, 20.0)
        completed = []
        for thresh, s in list(self._shadow_active.items()):
            if s["direction"] == "LONG":
                pts = price - s["entry_price"]
            else:
                pts = s["entry_price"] - price
            pnl = pts * pv
            if pts > s["mfe_pts"]:
                s["mfe_pts"] = round(pts, 2)
                s["mfe_dollars"] = round(pnl, 2)
            if pts < 0 and abs(pts) > s["mae_pts"]:
                s["mae_pts"] = round(abs(pts), 2)
                s["mae_dollars"] = round(abs(pnl), 2)
            if pnl >= DCA_TP_DOLLARS:
                s["exit_price"] = price
                s["exit_time"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
                s["final_pnl"] = round(pnl, 2)
                s["result"] = "TP"
                completed.append(thresh)

        for thresh in completed:
            entry = self._shadow_active.pop(thresh)
            self._shadow_log.append(entry)
            self._save_shadow_log()
            print(f"[SHADOW] {thresh:.0%} — TP HIT! {entry['direction']} "
                  f"entry {entry['entry_price']:.2f} -> exit {entry['exit_price']:.2f} "
                  f"| MFE ${entry['mfe_dollars']:.0f} MAE ${entry['mae_dollars']:.0f}")

    def shadow_clear_on_session_end(self):
        if not self._shadow_active:
            return
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        for thresh, s in self._shadow_active.items():
            s["exit_time"] = now_str
            s["result"] = "SESSION_END"
            self._shadow_log.append(s)
        count = len(self._shadow_active)
        self._shadow_active.clear()
        self._save_shadow_log()
        if count:
            print(f"[SHADOW] Session end — closed {count} active shadow entries")

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

    async def _reconnect_account(self, acc):
        """Reconnect a single account independently, preserve indicators."""
        cooldown = min(RECONNECT_COOLDOWN_BASE * (2 ** acc._reconnect_failures), RECONNECT_COOLDOWN_MAX)
        if time.time() - acc._last_reconnect < cooldown:
            return
        acc._last_reconnect = time.time()
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now_str}] [{acc.name} RECONNECT] attempt #{acc._reconnect_failures + 1}...")
        if acc.connected and acc.suite:
            try:
                await acc.suite.disconnect()
            except Exception:
                pass
            acc.connected = False
        await asyncio.sleep(2)
        try:
            await acc.connect([self.symbol])
            acc._reconnect_failures = 0
            acc._last_price_change_time = time.time()
            acc._gateway_logout = False
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{acc.name} RECONNECT] restored")
        except Exception as e:
            acc._reconnect_failures += 1
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{acc.name} RECONNECT] failed (#{acc._reconnect_failures}): {e}")

    async def _ensure_connection(self, acc):
        """Check if account needs reconnection and handle it."""
        if not in_session() or in_blackout():
            return
        now = time.time()
        need_reconnect = False
        reason = ""
        if getattr(acc, '_gateway_logout', False):
            need_reconnect = True
            reason = "GatewayLogout"
            acc._gateway_logout = False
        elif acc.connected and (now - acc._last_price_change_time) > FROZEN_FEED_THRESHOLD:
            need_reconnect = True
            reason = f"frozen feed ({now - acc._last_price_change_time:.0f}s)"
        elif not acc.connected:
            need_reconnect = True
            reason = "disconnected"
        if need_reconnect:
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{acc.name}] Connection issue: {reason} — reconnecting")
            await self._reconnect_account(acc)

    async def _auto_reconnect(self):
        """Reconnect all accounts (fallback for global issues)."""
        self._reconnecting = True
        self._last_reconnect_time = time.time()
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now_str}] [RECONNECT] Global reconnect, indicators preserved...")
        for acc in self.accounts:
            await self._reconnect_account(acc)
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
        bull_str = f"{self.engine._mss_bull_level:.1f}" if self.engine._mss_bull_level else "-"
        bear_str = f"{self.engine._mss_bear_level:.1f}" if self.engine._mss_bear_level else "-"
        msg = (f"HEARTBEAT|{now_str} ET\n"
               f"MSS bull={bull_str} bear={bear_str} | Bricks: {len(self.engine.renko.bricks)}\n"
               + "\n".join(acct_lines))
        threading.Thread(target=send_telegram,
                         args=(self.tg_token, self.tg_chat, msg),
                         daemon=True).start()

    async def _broadcast_entry(self, direction, price, features, rl_idx, rl_params, mss_bb_pct=1.0, mss_level=None):
        """Place entry on accounts whose BB threshold is met."""
        tasks = []
        for acc in self.accounts:
            if not acc.connected or acc.position != 0:
                continue
            if abs(acc.daily_loss) >= DAILY_LOSS_LIMIT:
                continue
            # For SHORT: mss_bb_pct is how high in BB band (0.8 = 80% below)
            # For LONG: mss_bb_pct is how low in BB band (0.2 = 80% above)
            # Account threshold: higher = stricter for SHORT, lower = stricter for LONG
            if direction == "SHORT" and mss_bb_pct < acc.bb_threshold:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{acc.name} BB-SKIP] SHORT — MSS @ BB {mss_bb_pct:.0%} "
                      f"< threshold {acc.bb_threshold:.0%}")
                continue
            if direction == "LONG" and mss_bb_pct > (1.0 - acc.bb_threshold):
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{acc.name} BB-SKIP] LONG — MSS @ BB {mss_bb_pct:.0%} "
                      f"> threshold {1.0 - acc.bb_threshold:.0%}")
                continue
            if acc.channel_filter and not (acc.reverse_mode or acc.trendline_mode or acc.bb_reversal_mode or acc.hvn_trail_mode):
                ch = self.engine.channel_state(CHANNEL_LOOKBACK, CHANNEL_SLOPE_MIN)
                if ch:
                    slope, ch_upper, ch_lower, ch_mid = ch
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    if direction == "SHORT" and slope > CHANNEL_SLOPE_MIN and price > ch_upper:
                        print(f"[{now_str}] [{acc.name} CH-SKIP] SHORT — price {price:.2f} above "
                              f"rising channel (slope {slope:+.3f}, upper {ch_upper:.2f})")
                        continue
                    if direction == "LONG" and slope < -CHANNEL_SLOPE_MIN and price < ch_lower:
                        print(f"[{now_str}] [{acc.name} CH-SKIP] LONG — price {price:.2f} below "
                              f"falling channel (slope {slope:+.3f}, lower {ch_lower:.2f})")
                        continue
                    ch_dir = "UP" if slope > CHANNEL_SLOPE_MIN else ("DN" if slope < -CHANNEL_SLOPE_MIN else "FLAT")
                    print(f"[{now_str}] [{acc.name} CH-OK] {direction} — channel {ch_dir} "
                          f"(slope {slope:+.3f}) | price {price:.2f} in [{ch_lower:.2f}-{ch_upper:.2f}]")
            if acc.volume_profile and mss_level is not None and not (acc.reverse_mode or acc.trendline_mode or acc.bb_reversal_mode):
                is_hvn, vol, pct, poc = self.engine.volume_profile_check(mss_level)
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                if not is_hvn:
                    if acc.vp_defer and (acc.pullback_pts > 0 or acc.hvn_entry):
                        hvn_target = self.engine.find_nearest_hvn(price, direction)
                        if hvn_target is not None:
                            if acc.hvn_entry:
                                offset = -acc.hvn_offset if direction == "LONG" else acc.hvn_offset
                                entry_level = hvn_target + offset
                                print(f"[{now_str}] [{acc.name} VP-DEFER] {direction} — MSS {mss_level:.2f} "
                                      f"thin zone ({pct:.0%} pctl) | waiting for HVN {hvn_target:.0f} "
                                      f"+ {acc.hvn_offset}pt = {entry_level:.0f}")
                                acc._pending_pullback = {
                                    "direction": direction,
                                    "signal_price": price,
                                    "original_signal": price,
                                    "features": features,
                                    "rl_idx": rl_idx,
                                    "rl_params": rl_params,
                                    "mss_level": mss_level,
                                    "time": time.time(),
                                    "hvn_entry_level": entry_level,
                                    "vp_deferred": True,
                                }
                            else:
                                print(f"[{now_str}] [{acc.name} VP-DEFER] {direction} — MSS {mss_level:.2f} "
                                      f"thin zone ({pct:.0%} pctl) | waiting for HVN {hvn_target:.0f} "
                                      f"+ {acc.pullback_pts}pt pullback")
                                acc._pending_pullback = {
                                    "direction": direction,
                                    "signal_price": hvn_target,
                                    "original_signal": price,
                                    "features": features,
                                    "rl_idx": rl_idx,
                                    "rl_params": rl_params,
                                    "mss_level": mss_level,
                                    "time": time.time(),
                                    "vp_deferred": True,
                                }
                            continue
                        else:
                            print(f"[{now_str}] [{acc.name} VP-SKIP] {direction} — MSS {mss_level:.2f} "
                                  f"at thin zone ({pct:.0%} pctl, {vol} TPOs) | no HVN found to defer")
                            continue
                    print(f"[{now_str}] [{acc.name} VP-SKIP] {direction} — MSS {mss_level:.2f} "
                          f"at thin zone ({pct:.0%} pctl, {vol} TPOs) | POC={poc:.0f}")
                    continue
                print(f"[{now_str}] [{acc.name} VP-OK] {direction} — MSS {mss_level:.2f} "
                      f"at HVN ({pct:.0%} pctl, {vol} TPOs) | POC={poc:.0f}")
            hull_result = self.engine.hull_ma_signal() if (acc.hull_filter and not (acc.reverse_mode or acc.trendline_mode or acc.bb_reversal_mode)) else None
            if hull_result is not None:
                hull_sig, hull_fresh = hull_result
                hull_agrees = (direction == "LONG" and hull_sig == "BUY") or \
                              (direction == "SHORT" and hull_sig == "SELL")
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                if not hull_agrees and hull_fresh:
                    print(f"[{now_str}] [{acc.name} HULL-WAIT] {direction} signal but Hull JUST flipped to {hull_sig} "
                          f"— deferring until Hull confirms")
                    acc._pending_pullback = {
                        "direction": direction,
                        "signal_price": price,
                        "original_signal": price,
                        "features": features,
                        "rl_idx": rl_idx,
                        "rl_params": rl_params,
                        "mss_level": mss_level,
                        "time": time.time(),
                        "hull_deferred": True,
                    }
                    continue
                elif not hull_agrees:
                    print(f"[{now_str}] [{acc.name} HULL-STALE] {direction} — Hull={hull_sig} but flipped a while ago, allowing entry")
                else:
                    print(f"[{now_str}] [{acc.name} HULL-OK] {direction} — Hull={hull_sig} confirms")
            if acc.hvn_entry:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                hvn_target = self.engine.find_nearest_hvn(price, direction)
                if hvn_target is not None:
                    offset = -acc.hvn_offset if direction == "LONG" else acc.hvn_offset
                    entry_level = hvn_target + offset
                    acc._pending_pullback = {
                        "direction": direction,
                        "signal_price": price,
                        "original_signal": price,
                        "features": features,
                        "rl_idx": rl_idx,
                        "rl_params": rl_params,
                        "mss_level": mss_level,
                        "time": time.time(),
                        "hvn_entry_level": entry_level,
                    }
                    print(f"[{now_str}] [{acc.name} HVN-ENTRY-WAIT] {direction} signal @ {price:.2f} "
                          f"— enter at 1st HVN {hvn_target:.0f} + {acc.hvn_offset}pt = {entry_level:.0f}")
                    continue
                else:
                    print(f"[{now_str}] [{acc.name} HVN-ENTRY] {direction} — no HVN found, using signal price")
            elif acc.pullback_pts > 0:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                acc._pending_pullback = {
                    "direction": direction,
                    "signal_price": price,
                    "features": features,
                    "rl_idx": rl_idx,
                    "rl_params": rl_params,
                    "mss_level": mss_level,
                    "time": time.time(),
                }
                print(f"[{now_str}] [{acc.name} PULLBACK-WAIT] {direction} signal @ {price:.2f} "
                      f"— waiting for {acc.pullback_pts}pt pullback")
                continue
            state_5m = self.engine.get_5m_state()
            if state_5m["ranging"]:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{acc.name} 5M-RANGE] {direction} — 5m is ranging [{state_5m['range_low']:.0f}-{state_5m['range_high']:.0f}], allowing both sides")
            elif state_5m["trend"]:
                if direction == "LONG" and state_5m["trend"] == "d":
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{acc.name} 5M-SKIP] LONG blocked — 5m trend is DOWN, only taking shorts")
                    continue
                if direction == "SHORT" and state_5m["trend"] == "u":
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{acc.name} 5M-SKIP] SHORT blocked — 5m trend is UP, only taking longs")
                    continue
            if not self.engine.filter_candle_agrees(direction):
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                forming = self.engine.get_filter_candle_color() or "doji"
                need = "green" if direction == "LONG" else "red"
                if not acc._pending_pullback or not acc._pending_pullback.get("filter_candle_wait"):
                    acc._pending_pullback = {
                        "direction": direction, "signal_price": price, "original_signal": price,
                        "features": features, "rl_idx": rl_idx, "rl_params": rl_params,
                        "mss_level": mss_level, "time": time.time(), "filter_candle_wait": True,
                    }
                    print(f"[{now_str}] [{acc.name} 3M-WAIT] {direction} signal — "
                          f"forming 3m is {forming.upper()}, need {need.upper()} (real-time)")
                continue
            if acc.hvn_trail_mode:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                trend = self.engine.trendline.get_trend()
                if acc._pending_pullback and acc._pending_pullback.get("hvn_trail_wait"):
                    pb = acc._pending_pullback
                    pullback_level = pb["pullback_level"]
                    hit = (pb["direction"] == "LONG" and price <= pullback_level) or \
                          (pb["direction"] == "SHORT" and price >= pullback_level)
                    if hit:
                        trend_ok = (pb["direction"] == "LONG" and trend == "u") or \
                                   (pb["direction"] == "SHORT" and trend == "d")
                        if trend_ok:
                            hvn_levels = []
                            for nth in range(1, 5):
                                hvn = self.engine.find_nearest_hvn(price, pb["direction"], nth=nth)
                                if hvn and hvn not in hvn_levels:
                                    hvn_levels.append(hvn)
                            if hvn_levels:
                                first_tp = hvn_levels[0]
                                if pb["direction"] == "LONG":
                                    sl_price = price - 15
                                else:
                                    sl_price = price + 15
                                tp_pts = abs(first_tp - price)
                                if not self.engine.filter_candle_agrees(pb["direction"]):
                                    forming = self.engine.get_filter_candle_color() or "doji"
                                    need = "green" if pb["direction"] == "LONG" else "red"
                                    print(f"[{now_str}] [{acc.name} HVN-TRAIL-3M-BLOCK] {pb['direction']} — "
                                          f"3m is {forming.upper()}, need {need.upper()}")
                                    continue
                                print(f"[{now_str}] [{acc.name} HVN-TRAIL-ENTRY] {pb['direction']} @ {price:.2f} | "
                                      f"trend={trend} confirmed at pullback | HVN targets: {[f'{h:.0f}' for h in hvn_levels]} | "
                                      f"TP1={first_tp:.2f} ({tp_pts:.0f}pts)")
                                acc._reverse_tp_price = first_tp
                                acc._reverse_sl_price = sl_price
                                acc._entry_trend = trend
                                acc._hvn_tp_levels = hvn_levels
                                acc._hvn_tp_current_idx = 0
                                if pb["direction"] == "LONG":
                                    success = await acc.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                                else:
                                    success = await acc.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                                if success:
                                    acc._pending_pullback = None
                            else:
                                print(f"[{now_str}] [{acc.name} HVN-TRAIL-SKIP] No HVN levels found")
                                acc._pending_pullback = None
                        else:
                            pass
                    elif time.time() - pb["time"] > 300:
                        acc._pending_pullback = None
                else:
                    if direction == "LONG":
                        pullback_level = price - 10
                    else:
                        pullback_level = price + 10
                    acc._pending_pullback = {
                        "direction": direction, "signal_price": price, "original_signal": price,
                        "features": features, "rl_idx": rl_idx, "rl_params": rl_params,
                        "mss_level": mss_level, "time": time.time(),
                        "hvn_trail_wait": True, "pullback_level": pullback_level,
                    }
                    print(f"[{now_str}] [{acc.name} HVN-TRAIL-WAIT] {direction} signal @ {price:.2f} | "
                          f"waiting for 10pt pullback to {pullback_level:.2f} + trendline confirm")
                continue
            if acc.bb_reversal_mode:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                setup = self.engine.check_bb_reversal_setup(price)
                if setup:
                    acc._bb_rev_setup = setup
                    print(f"[{now_str}] [{acc.name} BB-REV-SETUP] {setup['type']} — "
                          f"full candle closed beyond BB(1.5), waiting for trendline break")
                trend = self.engine.trendline.get_trend()
                if acc._bb_rev_setup:
                    setup = acc._bb_rev_setup
                    if setup["type"] == "SHORT_SETUP" and trend != "d":
                        continue
                    if setup["type"] == "LONG_SETUP" and trend != "u":
                        continue
                    entry_dir = "SHORT" if setup["type"] == "SHORT_SETUP" else "LONG"
                    bb_mid = setup["bb_mid"]
                    if not self.engine.filter_candle_agrees(entry_dir):
                        bb_candle_color = self.engine.get_filter_candle_color() or "doji"
                        need = "red" if entry_dir == "SHORT" else "green"
                        print(f"[{now_str}] [{acc.name} BB-REV-3M-BLOCK] {entry_dir} blocked — "
                              f"3m candle is {bb_candle_color.upper()}, need {need.upper()}")
                        continue
                    if entry_dir == "SHORT":
                        sl_price = setup.get("candle_high", price + 10) + 2
                        tp_price = bb_mid
                    else:
                        sl_price = setup.get("candle_low", price - 10) - 2
                        tp_price = bb_mid
                    sl_pts = abs(price - sl_price)
                    tp_pts = abs(tp_price - price)
                    if tp_pts < 3:
                        continue
                    print(f"[{now_str}] [{acc.name} BB-REV-ENTRY] {entry_dir} @ {price:.2f} | "
                          f"trend={trend} confirmed | BB mid={bb_mid:.2f} | "
                          f"3m={self.engine.get_filter_candle_color() or 'doji'} | "
                          f"TP={tp_price:.2f} ({tp_pts:.0f}pts) SL={sl_price:.2f} ({sl_pts:.0f}pts)")
                    acc._reverse_tp_price = tp_price
                    acc._reverse_sl_price = sl_price
                    acc._entry_trend = trend
                    acc._bb_rev_setup = None
                    if entry_dir == "LONG":
                        tasks.append(acc.enter_long(price, features, rl_idx, rl_params, mss_level=mss_level))
                    else:
                        tasks.append(acc.enter_short(price, features, rl_idx, rl_params, mss_level=mss_level))
                continue
            if acc.trendline_mode:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                trend = self.engine.trendline.get_trend()
                tl_price = self.engine.trendline.get_trend_line_price()
                vp_levels = self.engine.pavp.get_levels()
                poc = vp_levels.get("poc")
                vah = vp_levels.get("vah")
                val = vp_levels.get("val")
                if direction == "LONG" and trend != "u":
                    if trend == "d":
                        print(f"[{now_str}] [{acc.name} TL-SKIP] LONG blocked — downtrend active (TL @ {tl_price:.2f})")
                    else:
                        print(f"[{now_str}] [{acc.name} TL-SKIP] LONG — no confirmed trend")
                    continue
                if direction == "SHORT" and trend != "d":
                    if trend == "u":
                        print(f"[{now_str}] [{acc.name} TL-SKIP] SHORT blocked — uptrend active (TL @ {tl_price:.2f})")
                    else:
                        print(f"[{now_str}] [{acc.name} TL-SKIP] SHORT — no confirmed trend")
                    continue
                if direction == "LONG":
                    sl_price = price - 15
                    tp_price = poc if poc and poc > price else (vah if vah and vah > price else price + 20)
                else:
                    sl_price = price + 15
                    tp_price = poc if poc and poc < price else (val if val and val < price else price - 20)
                sl_pts = abs(price - sl_price)
                tp_pts = abs(tp_price - price)
                features["trendline"] = trend
                features["tl_price"] = round(tl_price, 2) if tl_price else 0
                ml_ok, ml_reason = self.engine.ml.should_enter(features)
                if not ml_ok:
                    print(f"[{now_str}] [{acc.name} TL-ML-SKIP] {direction} — {ml_reason}")
                    continue
                poc_str = f"POC={poc:.2f}" if poc else "noPOC"
                print(f"[{now_str}] [{acc.name} TL-ENTRY] {direction} @ {price:.2f} | "
                      f"trend={trend} TL={tl_price:.2f} | {poc_str} | "
                      f"TP={tp_price:.2f} ({tp_pts:.0f}pts) SL={sl_price:.2f} ({sl_pts:.0f}pts) | {ml_reason}")
                acc._reverse_tp_price = tp_price
                acc._reverse_sl_price = sl_price
                acc._entry_trend = trend
                if direction == "LONG":
                    tasks.append(acc.enter_long(price, features, rl_idx, rl_params, mss_level=mss_level))
                else:
                    tasks.append(acc.enter_short(price, features, rl_idx, rl_params, mss_level=mss_level))
                continue
            if acc.pavp_mode:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                pavp_levels = self.engine.pavp.get_levels()
                poc = pavp_levels.get("poc")
                vah = pavp_levels.get("vah")
                val = pavp_levels.get("val")
                if poc is None:
                    print(f"[{now_str}] [{acc.name} PAVP-SKIP] {direction} — no PAVP data yet")
                    continue
                if direction == "LONG":
                    if price < val:
                        pavp_zone = "below_val"
                    elif price < poc:
                        pavp_zone = "below_poc"
                    else:
                        pavp_zone = "above_poc"
                    sl_price = val - 2 if val else price - 15
                    tp_price = poc if poc > price else vah if vah else price + 20
                else:
                    if price > vah:
                        pavp_zone = "above_vah"
                    elif price > poc:
                        pavp_zone = "above_poc"
                    else:
                        pavp_zone = "below_poc"
                    sl_price = vah + 2 if vah else price + 15
                    tp_price = poc if poc < price else val if val else price - 20
                sl_pts = abs(price - sl_price)
                tp_pts = abs(tp_price - price)
                if tp_pts < 3:
                    print(f"[{now_str}] [{acc.name} PAVP-SKIP] {direction} — TP too close ({tp_pts:.1f}pts)")
                    continue
                features["pavp_zone"] = pavp_zone
                features["pavp_poc_dist"] = round(price - poc, 2)
                features["pavp_sl_pts"] = round(sl_pts, 1)
                features["pavp_tp_pts"] = round(tp_pts, 1)
                ml_ok, ml_reason = self.engine.ml.should_enter(features)
                if not ml_ok:
                    print(f"[{now_str}] [{acc.name} PAVP-ML-SKIP] {direction} — {ml_reason} | zone={pavp_zone}")
                    self.engine.ml.record_trade(features, 0, source="skip", entered=False)
                    continue
                print(f"[{now_str}] [{acc.name} PAVP-ENTRY] {direction} @ {price:.2f} | "
                      f"POC={poc:.2f} VAH={vah:.2f} VAL={val:.2f} | zone={pavp_zone} | "
                      f"TP={tp_price:.2f} ({tp_pts:.0f}pts) SL={sl_price:.2f} ({sl_pts:.0f}pts) | {ml_reason}")
                acc._reverse_tp_price = tp_price
                acc._reverse_sl_price = sl_price
                if direction == "LONG":
                    tasks.append(acc.enter_long(price, features, rl_idx, rl_params, mss_level=mss_level))
                else:
                    tasks.append(acc.enter_short(price, features, rl_idx, rl_params, mss_level=mss_level))
                continue
            entry_dir = direction
            if acc.reverse_mode:
                entry_dir = "SHORT" if direction == "LONG" else "LONG"
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                trend_15m = self.engine.trend_15m() if not getattr(acc, 'no_15m_filter', False) else None
                if trend_15m is not None:
                    trend_allows = (trend_15m == "BULL" and entry_dir == "SHORT") or \
                                   (trend_15m == "BEAR" and entry_dir == "LONG")
                    if not trend_allows:
                        print(f"[{now_str}] [{acc.name} 15M-SKIP] {entry_dir} blocked — 15m trend is {trend_15m}, "
                              f"only allowing pullbacks WITH the trend")
                        continue
                if direction == "LONG":
                    rev_entry_level = price - acc.reverse_entry_pts
                else:
                    rev_entry_level = price + acc.reverse_entry_pts
                acc._pending_pullback = {
                    "direction": entry_dir,
                    "signal_price": price,
                    "original_signal": price,
                    "features": features,
                    "rl_idx": rl_idx,
                    "rl_params": rl_params,
                    "mss_level": mss_level,
                    "time": time.time(),
                    "reverse_entry_level": rev_entry_level,
                }
                trend_str = f" | 15m={trend_15m}" if trend_15m else ""
                print(f"[{now_str}] [{acc.name} REV-WAIT] Signal={direction} -> {entry_dir} | "
                      f"waiting for {acc.reverse_entry_pts}pt pullback to {rev_entry_level:.2f}{trend_str}")
                continue
            if entry_dir == "LONG":
                tasks.append(acc.enter_long(price, features, rl_idx, rl_params, mss_level=mss_level))
            else:
                tasks.append(acc.enter_short(price, features, rl_idx, rl_params, mss_level=mss_level))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._is_leader:
                self._broadcast_trade_signal("enter", direction, price, features, rl_idx, rl_params, mss_level)
            for acc in self.accounts:
                if acc.connected and acc.position != 0:
                    asyncio.ensure_future(acc.record_l2_snapshot(f"ENTRY-{direction}", price, self._l2_file))

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
            if self._is_leader and direction_filter:
                self._broadcast_trade_signal("exit", direction_filter, price)

    async def run(self):
        print(f"[BOT] Multi-Account Renko MFI+MSS Bot")
        print(f"[BOT] Brick: {BRICK_SIZE}pt | MFI({MFI_PERIOD}) OB={MFI_OVERBOUGHT}/OS={MFI_OVERSOLD}")
        print(f"[BOT] TP=${DCA_TP_DOLLARS} | DCA at HVN zones (10pt past) | max {DCA_MAX_CONTRACTS} contracts | Hull({HULL_LENGTH}) filter ON")
        print(f"[BOT] Session: {TRADE_SESSION_START.strftime('%H:%M')} - "
              f"{TRADE_SESSION_END.strftime('%H:%M')} ET")
        print(f"[BOT] Accounts: {[a.name for a in self.accounts]}")

        self.load_all_state()
        if self._engine_sync:
            self._download_engine_from_s3()
        await self.connect_all()

        # Use first connected account for price feed
        primary_acc = next((a for a in self.accounts if a.connected), None)
        if not primary_acc:
            raise RuntimeError("No connected accounts for price feed")

        price = await primary_acc.ctx.data.get_current_price()
        print(f"[BOT] {self.symbol} price: {price:.2f}" if price else f"[BOT] {self.symbol}: market closed")

        acct_list = []
        for a in self.accounts:
            if not a.connected:
                continue
            if a.rl_tp_enabled:
                acct_list.append(f"{a.name}({a.account_name})\n  RL TP/SL (dynamic, {len(RL_TP_ACTIONS)} configs)")
            elif a.reverse_mode:
                tp_dollars = a.reverse_tp_pts * a.pv
                sl_dollars = a.reverse_sl_pts * a.pv
                filt = " no15m" if getattr(a, 'no_15m_filter', False) else " 15m"
                acct_list.append(f"{a.name}({a.account_name})\n  REVERSE TP=${tp_dollars:.0f}({a.reverse_tp_pts}pt) SL=${sl_dollars:.0f}({a.reverse_sl_pts}pt){filt}")
            else:
                acct_list.append(f"{a.name}({a.account_name})\n  TP=${DCA_TP_DOLLARS}")
        msg = (f"STATUS|Multi-Account Bot started\n"
               f"Accounts:\n" + "\n".join(acct_list) + f"\n"
               f"Brick: {BRICK_SIZE}pt")
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
                if _was_in_session:
                    self.shadow_clear_on_session_end()
                await asyncio.sleep(5)
                self.last_tick_time = time.time()
                _was_in_session = False
                continue

            if in_blackout():
                if _was_in_session:
                    self.shadow_clear_on_session_end()
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
                if getattr(acc, '_needs_crash_recovery', False):
                    await acc._crash_recovery_check(price)

            # 0. Check pending pullback entries
            for acc in self.accounts:
                if not acc.connected or not acc._pending_pullback or acc.position != 0:
                    continue
                pb = acc._pending_pullback
                if pb.get("hull_deferred"):
                    hull_result = self.engine.hull_ma_signal()
                    hull_agrees = False
                    hull_sig = None
                    if hull_result is not None:
                        hull_sig, _ = hull_result
                        hull_agrees = (pb["direction"] == "LONG" and hull_sig == "BUY") or \
                                      (pb["direction"] == "SHORT" and hull_sig == "SELL")
                    if hull_agrees:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        if not self.engine.filter_candle_agrees(pb["direction"]):
                            forming = self.engine.get_filter_candle_color() or "doji"
                            need = "green" if pb["direction"] == "LONG" else "red"
                            print(f"[{now_str}] [{acc.name} HULL-3M-WAIT] {pb['direction']} — "
                                  f"Hull agrees but 3m is {forming.upper()}, need {need.upper()}")
                            pb["hull_wait"] = False
                            pb["filter_candle_wait"] = True
                            continue
                        print(f"[{now_str}] [{acc.name} HULL-CONFIRMED] {pb['direction']} — "
                              f"Hull flipped to {hull_sig}, entering @ {price:.2f}")
                        if pb["direction"] == "LONG":
                            success = await acc.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                        else:
                            success = await acc.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                        if success:
                            acc._pending_pullback = None
                        else:
                            print(f"[{now_str}] [{acc.name} ENTRY-RETRY] Order failed — keeping signal, will retry")
                    else:
                        pv = acc.pv
                        ref_price = pb.get("original_signal", pb["signal_price"])
                        if pb["direction"] == "SHORT":
                            profit_move = (ref_price - price) * pv
                        else:
                            profit_move = (price - ref_price) * pv
                        if profit_move >= DCA_TP_DOLLARS:
                            now_str = datetime.now(ET).strftime("%H:%M:%S")
                            print(f"[{now_str}] [{acc.name} HULL-SKIP] {pb['direction']} — "
                                  f"price moved ${profit_move:.0f} profit while waiting for Hull, skipping")
                            acc._pending_pullback = None
                    continue
                if pb.get("filter_candle_wait"):
                    if self.engine.filter_candle_agrees(pb["direction"]):
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        forming = self.engine.get_filter_candle_color()
                        print(f"[{now_str}] [{acc.name} 3M-CONFIRMED] {pb['direction']} — "
                              f"forming 3m turned {forming}, entering @ {price:.2f}")
                        if pb["direction"] == "LONG":
                            success = await acc.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb.get("mss_level"))
                        else:
                            success = await acc.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb.get("mss_level"))
                        if success:
                            acc._pending_pullback = None
                    continue
                rev_level = pb.get("reverse_entry_level")
                if rev_level is not None:
                    sig_price = pb.get("original_signal", pb["signal_price"])
                    pv = acc.pv
                    if pb["direction"] == "SHORT":
                        hit = price <= rev_level
                        profit_from_signal = (sig_price - price) * pv
                    else:
                        hit = price >= rev_level
                        profit_from_signal = (price - sig_price) * pv
                    if hit:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        if not self.engine.filter_candle_agrees(pb["direction"]):
                            forming = self.engine.get_filter_candle_color() or "doji"
                            need = "green" if pb["direction"] == "LONG" else "red"
                            print(f"[{now_str}] [{acc.name} REV-3M-BLOCK] {pb['direction']} — "
                                  f"pullback hit but 3m is {forming.upper()}, need {need.upper()}")
                            continue
                        print(f"[{now_str}] [{acc.name} REV-ENTRY-HIT] {pb['direction']} — "
                              f"price {price:.2f} reached pullback zone {rev_level:.2f}, 3m confirmed, entering")
                        if pb["direction"] == "LONG":
                            success = await acc.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                        else:
                            success = await acc.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                        if success:
                            acc._pending_pullback = None
                    elif abs(profit_from_signal) >= DCA_TP_DOLLARS:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{acc.name} REV-ENTRY-SKIP] {pb['direction']} — "
                              f"price moved ${profit_from_signal:.0f} from signal, skipping")
                        acc._pending_pullback = None
                    continue
                hvn_level = pb.get("hvn_entry_level")
                if hvn_level is not None:
                    ref_price = pb.get("original_signal", pb["signal_price"])
                    pv = acc.pv
                    if pb["direction"] == "SHORT":
                        profit_move = (ref_price - price) * pv
                    else:
                        profit_move = (price - ref_price) * pv
                    hit = (pb["direction"] == "LONG" and price <= hvn_level) or \
                          (pb["direction"] == "SHORT" and price >= hvn_level)
                    if hit:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        if not self.engine.filter_candle_agrees(pb["direction"]):
                            forming = self.engine.get_filter_candle_color() or "doji"
                            need = "green" if pb["direction"] == "LONG" else "red"
                            print(f"[{now_str}] [{acc.name} HVN-3M-BLOCK] {pb['direction']} — "
                                  f"HVN hit but 3m is {forming.upper()}, need {need.upper()}")
                            continue
                        print(f"[{now_str}] [{acc.name} HVN-ENTRY-HIT] {pb['direction']} — "
                              f"price {price:.2f} reached HVN level {hvn_level:.0f}, 3m confirmed, entering")
                        if pb["direction"] == "LONG":
                            success = await acc.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                        else:
                            success = await acc.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                        if success:
                            acc._pending_pullback = None
                    elif profit_move >= DCA_TP_DOLLARS:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{acc.name} HVN-ENTRY-SKIP] {pb['direction']} — "
                              f"price ran ${profit_move:.0f} profit, skipping stale signal")
                        acc._pending_pullback = None
                    continue
                sig_price = pb["signal_price"]
                ref_price = pb.get("original_signal", sig_price)
                pv = acc.pv
                if pb["direction"] == "SHORT":
                    pullback_move = price - sig_price
                    profit_move = (ref_price - price) * pv
                else:
                    pullback_move = sig_price - price
                    profit_move = (price - ref_price) * pv
                if pullback_move >= acc.pullback_pts:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    if not self.engine.filter_candle_agrees(pb["direction"]):
                        forming = self.engine.get_filter_candle_color() or "doji"
                        need = "green" if pb["direction"] == "LONG" else "red"
                        label = "VP-DEFER-HIT" if pb.get("vp_deferred") else "PULLBACK-HIT"
                        print(f"[{now_str}] [{acc.name} {label}-3M-WAIT] {pb['direction']} — "
                              f"pullback hit but 3m is {forming.upper()}, need {need.upper()}")
                        pb["filter_candle_wait"] = True
                        continue
                    label = "VP-DEFER-HIT" if pb.get("vp_deferred") else "PULLBACK-HIT"
                    print(f"[{now_str}] [{acc.name} {label}] {pb['direction']} — "
                          f"pullback {pullback_move:.1f}pts from {sig_price:.2f}, entering @ {price:.2f}")
                    if pb["direction"] == "LONG":
                        success = await acc.enter_long(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                    else:
                        success = await acc.enter_short(price, pb["features"], pb["rl_idx"], pb["rl_params"], mss_level=pb["mss_level"])
                    if success:
                        acc._pending_pullback = None
                    else:
                        print(f"[{now_str}] [{acc.name} ENTRY-RETRY] Order failed — keeping signal, will retry")
                elif profit_move >= DCA_TP_DOLLARS:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{acc.name} PULLBACK-SKIP] {pb['direction']} — "
                          f"price went to ${profit_move:.0f} profit without pullback, skipping")
                    acc._pending_pullback = None

            # 1. Check per-account position exits (TP, trail, DCA)
            for acc in self.accounts:
                if not acc.connected:
                    continue
                acc._last_known_trend = self.engine.trendline.get_trend()
                acc._5m_mss_invalidated = self.engine.check_5m_mss_invalidation(acc.position, price) if acc.position != 0 else False
                pos_actions = acc.check_position(price)
                for act in pos_actions:
                    if act[0] == "flatten":
                        direction = "LONG" if acc.position == 1 else "SHORT"
                        asyncio.ensure_future(acc.record_l2_snapshot(f"EXIT-{act[2]}", act[1], self._l2_file))
                        await acc.flatten(act[1], act[2], signal_engine=self.engine)
                        if self._is_leader:
                            self._broadcast_trade_signal("exit", direction, act[1])
                    elif act[0] == "dca_hvn1":
                        await acc.dca_add(act[1])
                        if self._is_leader and acc.position != 0:
                            direction = "LONG" if acc.position == 1 else "SHORT"
                            self._broadcast_trade_signal("dca", direction, act[1])
                    elif act[0] == "flip":
                        await acc.flip_position(act[1], signal_engine=self.engine)
                # Set DCA1 HVN target for accounts in positions
                if acc.position != 0 and acc._dca1_hvn_level is None and not acc._dca_done:
                    direction = "LONG" if acc.position == 1 else "SHORT"
                    hvn = self.engine.find_nearest_hvn(price, direction, nth=acc.dca_hvn_nth)
                    if hvn is not None:
                        dca_offset = acc.hvn_offset if acc.hvn_entry else 10
                        offset = -dca_offset if acc.position == 1 else dca_offset
                        acc._dca1_hvn_level = hvn + offset
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        nth_str = f" ({ordinal(acc.dca_hvn_nth)} HVN)" if acc.dca_hvn_nth > 1 else ""
                        print(f"[{now_str}] [{acc.name} DCA1-TARGET] {direction} — "
                              f"HVN {hvn:.0f}, DCA1 at {acc._dca1_hvn_level:.0f} ({dca_offset}pt past){nth_str}")

            # 2. Feed signal engine and get entry/exit signals
            signals = self.engine.tick(price, self.last_tick_time)

            for sig in signals:
                if sig[0] == "enter_long":
                    _, ep, features, rl_idx, rl_params, mss_bb_pct, mss_level = sig
                    self.shadow_check_signal("LONG", ep, mss_bb_pct, mss_level)
                    any_in_position = any(a.position != 0 for a in self.accounts if a.connected)
                    if not any_in_position:
                        await self._broadcast_entry("LONG", ep, features, rl_idx, rl_params, mss_bb_pct, mss_level)
                elif sig[0] == "enter_short":
                    _, ep, features, rl_idx, rl_params, mss_bb_pct, mss_level = sig
                    self.shadow_check_signal("SHORT", ep, mss_bb_pct, mss_level)
                    any_in_position = any(a.position != 0 for a in self.accounts if a.connected)
                    if not any_in_position:
                        await self._broadcast_entry("SHORT", ep, features, rl_idx, rl_params, mss_bb_pct, mss_level)

            # 3. Update shadow tracking on every tick
            self.shadow_tick(price)

            now = time.time()

            # --- Per-account health check and reconnection ---
            for acc in self.accounts:
                await self._ensure_connection(acc)

            # --- Global stale data bailout ---
            tick_gap = now - self.last_real_tick_time
            if tick_gap > STALE_DATA_THRESHOLD and in_session() and not in_blackout() and not self._reconnecting:
                print(f"[HEALTH] Global stale: no real tick for {tick_gap:.0f}s — reconnecting all")
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

            # --- Periodic L2 orderbook recording ---
            if now - self._last_l2_snapshot > 60:
                self._last_l2_snapshot = now
                primary = next((a for a in self.accounts if a.connected), None)
                if primary and price:
                    asyncio.ensure_future(primary.record_l2_snapshot("PERIODIC", price, self._l2_file))

            # --- Periodic save + engine sync ---
            if now - last_save > 15:
                self.save_all_state()
                self._upload_engine_to_s3()
                self._sync_engine_from_s3()
                last_save = now

            # --- Status log ---
            if now - last_status > 300:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                brick_str = (f"Last brick: {self.engine.renko.last_direction() or 'none'} "
                             f"@ {self.engine.renko._last_close:.2f}") if self.engine.renko._last_close else "No bricks"
                bull_str = f"{self.engine._mss_bull_level:.1f}" if self.engine._mss_bull_level else "-"
                bear_str = f"{self.engine._mss_bear_level:.1f}" if self.engine._mss_bear_level else "-"
                vp_candles = len(self.engine._vp_candle_bins)
                vp_poc_str = ""
                if self.engine._vp_totals:
                    poc = max(self.engine._vp_totals, key=self.engine._vp_totals.get)
                    vp_poc_str = f" | VP:{vp_candles}c POC={poc:.0f}"
                print(f"\n  [{self.symbol} @ {now_str}] Price: {price:.2f} | {brick_str} | MSS bull={bull_str} bear={bear_str}{vp_poc_str}")
                print(f"    Bricks: {len(self.engine.renko.bricks)} | {self.engine.ml.stats()}")
                for acc in self.accounts:
                    if not acc.connected:
                        continue
                    pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[acc.position]
                    print(f"    [{acc.name}] {pos_str} x{acc.contracts_held} | P&L: ${acc.live_pnl:.2f}")
                if self._shadow_active:
                    shadow_parts = []
                    for t in sorted(self._shadow_active):
                        s = self._shadow_active[t]
                        shadow_parts.append(f"{t:.0%}:{s['direction'][0]}${s['mfe_dollars']:.0f}/-${s['mae_dollars']:.0f}")
                    print(f"    [SHADOW] {', '.join(shadow_parts)}")
                print(f"    [SHADOW LOG] {len(self._shadow_log)} completed trades")
                last_status = now

            # --- GC + memory ---
            if now - last_gc > 120:
                gc.collect()
                try:
                    import resource
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                except (ImportError, AttributeError):
                    import os as _os
                    try:
                        with open(f"/proc/{_os.getpid()}/status") as _f:
                            for _line in _f:
                                if _line.startswith("VmRSS:"):
                                    rss_mb = int(_line.split()[1]) / 1024
                                    break
                            else:
                                rss_mb = 0
                    except Exception:
                        rss_mb = 0
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
    parser.add_argument("--brick-size", type=float, default=0, help="Override brick size (0 = use default)")
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

    global BRICK_SIZE
    if args.brick_size > 0:
        BRICK_SIZE = args.brick_size

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
