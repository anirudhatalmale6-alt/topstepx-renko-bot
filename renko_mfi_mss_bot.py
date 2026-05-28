"""
TopstepX Renko MFI + MSS Bot (ML + RL)
============================================================
Strategy: 15s MSS trend shift + MFI exhaustion on 3pt Renko bricks confirms entry
- Build Renko bricks (3 pt brick size) from 1-second tick data
- MFI(14) on Renko bricks: oversold <= 20 (LONG), overbought >= 85 (SHORT)
- Entry: MFI crossing threshold sets pending signal, MSS on 15s candles confirms
  - Bearish MSS: ascending swing lows break down -> enter SHORT
  - Bullish MSS: descending swing highs break up -> enter LONG
- Exit: opposite MFI signal / TP limit / trailing profit / SL
- ML (logistic regression) skips bad setups and learns time-of-day patterns
- RL (Q-learning) optimizes SL, trail-activation, trail-pullback per market state
- DCA: adds 1 contract at -$80, max 2 contracts

Usage:
    python renko_mfi_mss_bot.py --symbols "NQ:1" --tick-interval 1
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
MSS_CANDLE_SECONDS = 15
BRICK_SIZE = 3.0

MFI_PERIOD = 14
MFI_OVERSOLD = 20.0
MFI_OVERBOUGHT = 85.0

# Fixed TP/SL (no RL — ML only filters entries)
DEFAULT_TP_PTS = 2
DEFAULT_SL_PTS = 20

# DCA strategy: enter 1 contract, add 1 more at -$80, TP at $20 total
DCA_TP_DOLLARS = 100.0
DCA_ADD_THRESHOLD = -80.0
DCA_MAX_CONTRACTS = 2

# Trailing profit: activates when unrealized >= threshold, trails back by trail_pct
TRAIL_PROFIT_ACTIVATE = 60.0   # activate trailing profit at $60 unrealized
TRAIL_PROFIT_PULLBACK = 0.40   # allow 40% pullback from peak (keeps 60%)

DAILY_LOSS_LIMIT = 1000.0


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


MSS_SWING_LOOKBACK = 5
MSS_MIN_SWING_PTS = 6.0
MSS_WARMUP_BRICKS = 15


class MSSDetector:
    """Detects Market Structure Shift on Renko bricks.
    Swing points = direction reversals (natural for Renko).
    Bearish MSS: ascending swing lows broken to downside.
    Bullish MSS: descending swing highs broken to upside."""

    def __init__(self):
        self.swing_highs = []
        self.swing_lows = []
        self._bearish_triggered = False
        self._bullish_triggered = False

    def update_swings(self, bricks: list):
        if len(bricks) < MSS_WARMUP_BRICKS:
            return
        highs = []
        lows = []
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

    def check(self, price: float) -> str:
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

    def status(self) -> str:
        sh = f"SH={[round(h, 2) for h in self.swing_highs[-3:]]}" if self.swing_highs else "SH=[]"
        sl = f"SL={[round(l, 2) for l in self.swing_lows[-3:]]}" if self.swing_lows else "SL=[]"
        return f"{sh} {sl}"


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
# Reinforcement Learning for Renko MSS
# ============================================================

ML_WARMUP_TRADES = 15
ML_LEARNING_RATE = 0.05
ML_SKIP_THRESHOLD = 0.25


class TradeFilter:
    """Online logistic regression — learns to skip bad setups."""

    N_FEATURES = 9

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
        now_et = datetime.now(ET)
        hour = now_et.hour
        minute = now_et.minute
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
            "minute": minute,
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
# Reinforcement Learning — Parameter Optimizer
# ============================================================

RL_ACTIONS = [
    {"sl_pts": 10, "trail_activate": 40.0, "trail_pullback": 0.25, "dca_threshold": -50.0, "label": "tight"},
    {"sl_pts": 15, "trail_activate": 60.0, "trail_pullback": 0.30, "dca_threshold": -80.0, "label": "conservative"},
    {"sl_pts": 20, "trail_activate": 80.0, "trail_pullback": 0.35, "dca_threshold": -100.0, "label": "balanced"},
    {"sl_pts": 30, "trail_activate": 100.0, "trail_pullback": 0.40, "dca_threshold": -120.0, "label": "moderate"},
    {"sl_pts": 40, "trail_activate": 150.0, "trail_pullback": 0.45, "dca_threshold": -150.0, "label": "wide"},
    {"sl_pts": 50, "trail_activate": 200.0, "trail_pullback": 0.50, "dca_threshold": None, "label": "runner"},
]

RL_WARMUP = 20
RL_EPSILON_START = 0.30
RL_EPSILON_MIN = 0.10
RL_EPSILON_DECAY = 0.995
RL_LR = 0.10


class ParamRL:
    """Q-learning — learns optimal SL, trail-activate, trail-pullback per market state."""

    def __init__(self, data_file: str):
        self.data_file = data_file
        self.n_actions = len(RL_ACTIONS)
        self.q_table = {}
        self.epsilon = RL_EPSILON_START
        self.total_trades = 0
        self._load()

    def _state_key(self, features: dict) -> str:
        consec = features.get("consecutive_bricks", 1)
        vol = "trending" if consec >= 8 else ("normal" if consec >= 3 else "choppy")
        mom = features.get("momentum_pts", 0.0)
        mom_s = "up" if mom > 2 else ("down" if mom < -2 else "flat")
        h = features.get("hour", 12)
        h_s = "off" if (h < 10 or h >= 18) else ("morning" if h < 12 else ("midday" if h < 14 else "afternoon"))
        return f"{vol}_{mom_s}_{h_s}"

    def choose(self, features: dict) -> tuple:
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

    def update(self, features: dict, action_idx: int, reward: float):
        key = self._state_key(features)
        if key not in self.q_table:
            self.q_table[key] = [0.0] * self.n_actions
        old = self.q_table[key][action_idx]
        self.q_table[key][action_idx] = old + RL_LR * (reward - old)
        self.total_trades += 1
        self.epsilon = max(RL_EPSILON_MIN, self.epsilon * RL_EPSILON_DECAY)
        self._save()
        best_idx = self.q_table[key].index(max(self.q_table[key]))
        print(f"[RL] state={key} action={RL_ACTIONS[action_idx]['label']} "
              f"reward=${reward:.2f} | best={RL_ACTIONS[best_idx]['label']} "
              f"eps={self.epsilon:.3f} ({self.total_trades} trades)")

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
            except Exception as e:
                print(f"[RL] Load error: {e}")

    def _save(self):
        try:
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"q_table": self.q_table, "epsilon": self.epsilon,
                           "total_trades": self.total_trades}, f)
            os.replace(tmp, self.data_file)
        except Exception as e:
            print(f"[RL] Save error: {e}")


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
        self.bb_candles = CandleBuilder(MSS_CANDLE_SECONDS)
        self.mss = MSSDetector()
        self.last_price = 0.0
        self.last_tick_time = 0.0
        self._prev_brick_dir = None
        self._pending_mss = None  # "bearish" or "bullish" — set by MSS, waiting for MFI confirm

        # MFI state
        self.brick_closes = []
        self.brick_opens = []
        self.brick_typicals = []
        self.brick_volumes = []
        self.mfi_value = None
        self.prev_mfi_value = None
        self._max_brick_history = 250
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
        self.rl = ParamRL(os.path.join(os.getcwd(), f"rl_state_{symbol}.json"))
        self._rl_action_idx = None
        self._rl_params = None
        self._tp_lock_pnl = 0.0

        self.ctx = None
        self._suite_client = None
        self._pending_rl = None
        self._start_balance = None
        self._pnl_session_day = None

        self.daily_loss = 0.0

    def extract_features(self) -> dict:
        return self.ml.extract_features(price=self.last_price, renko=self.renko)

    def _add_brick_data(self, brick_open: float, brick_close: float):
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

    def _calc_mfi(self) -> str:
        """Calculate MFI(14) and return 'oversold', 'overbought', or None on crossing."""
        n = len(self.brick_typicals)
        if n < MFI_PERIOD + 1:
            return None
        typicals = self.brick_typicals[-(MFI_PERIOD + 1):]
        volumes = self.brick_volumes[-(MFI_PERIOD + 1):]
        pos_flow = 0.0
        neg_flow = 0.0
        for i in range(1, len(typicals)):
            raw_flow = typicals[i] * volumes[i]
            if typicals[i] > typicals[i - 1]:
                pos_flow += raw_flow
            elif typicals[i] < typicals[i - 1]:
                neg_flow += raw_flow
        if neg_flow < 1e-10:
            new_mfi = 100.0
        else:
            ratio = pos_flow / neg_flow
            new_mfi = 100.0 - (100.0 / (1.0 + ratio))
        signal = None
        if self.prev_mfi_value is not None:
            if new_mfi <= MFI_OVERSOLD and self.prev_mfi_value > MFI_OVERSOLD:
                signal = "oversold"
            elif new_mfi >= MFI_OVERBOUGHT and self.prev_mfi_value < MFI_OVERBOUGHT:
                signal = "overbought"
        self.prev_mfi_value = self.mfi_value if self.mfi_value is not None else new_mfi
        self.mfi_value = new_mfi
        return signal

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
        self._rl_action_idx, self._rl_params = self.rl.choose(features)

        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG x{qty} @ {price:.2f} | "
              f"TP=${DCA_TP_DOLLARS} | SL={self._rl_params['sl_pts']}pts "
              f"({self._rl_params['label']}) | trail=${self._rl_params['trail_activate']:.0f}/"
              f"{100*self._rl_params['trail_pullback']:.0f}% | "
              f"DCA at -${abs(DCA_ADD_THRESHOLD)} | Session P&L: ${self.live_pnl:.2f}")

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
                self.active_sl_pts = self._rl_params["sl_pts"]
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._tp_lock_pnl = 0.0
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
        self._rl_action_idx, self._rl_params = self.rl.choose(features)

        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT x{qty} @ {price:.2f} | "
              f"TP=${DCA_TP_DOLLARS} | SL={self._rl_params['sl_pts']}pts "
              f"({self._rl_params['label']}) | trail=${self._rl_params['trail_activate']:.0f}/"
              f"{100*self._rl_params['trail_pullback']:.0f}% | "
              f"DCA at -${abs(DCA_ADD_THRESHOLD)} | Session P&L: ${self.live_pnl:.2f}")

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
                self.active_sl_pts = self._rl_params["sl_pts"]
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._tp_lock_pnl = 0.0
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

        real_unr = await self._get_real_unrealized()
        if real_unr is not None:
            if self.position == 1:
                est_unr = (price - self.entry_price) * self.pv * self.contracts_held
            else:
                est_unr = (self.entry_price - price) * self.pv * self.contracts_held
            if abs(real_unr - est_unr) > 5.0:
                print(f"[{now_str}] [{self.symbol} PNL-CHECK] est=${est_unr:.2f} real=${real_unr:.2f} "
                      f"(diff ${real_unr - est_unr:.2f})")

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

        rl_idx = self._rl_action_idx
        rl_feat = self.entry_features

        self.position = 0
        self.contracts_held = 0
        self._tp_limit_order_id = None
        self._tp_limit_price = None
        self._trail_profit_active = False
        self._trail_profit_peak = 0.0
        self._trail_profit_floor = 0.0
        self._tp_lock_pnl = 0.0

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

        feat = self._pending_rl["features"] if self._pending_rl else rl_feat
        m = self._pending_rl.get("mae", self.trade_mae) if self._pending_rl else self.trade_mae
        mf = self._pending_rl.get("mfe", self.trade_mfe) if self._pending_rl else self.trade_mfe

        if feat:
            self.ml.record_trade(feat, trade_pnl, source="live",
                                 entered=True, mae=m, mfe=mf)
            if rl_idx is not None:
                self.rl.update(feat, rl_idx, trade_pnl)

        self._pending_rl = None
        self.entry_features = None
        self._rl_action_idx = None
        self._rl_params = None

    def tick(self, price: float, ts: float = None):
        """Process a tick. MSS shift first, then MFI exhaustion confirms entry."""
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

        self.candles.feed(price, ts)
        self.bb_candles.feed(price, ts)
        new_bricks = self.renko.feed(price)

        if self.position != 0:
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.pv * self.contracts_held
            else:
                unrealized = (self.entry_price - price) * self.pv * self.contracts_held
            self.trade_mfe = max(self.trade_mfe, unrealized)
            self.trade_mae = min(self.trade_mae, unrealized)

            # RL-chosen parameters (fall back to defaults if no RL params set)
            trail_act = self._rl_params["trail_activate"] if self._rl_params else TRAIL_PROFIT_ACTIVATE
            trail_pb = self._rl_params["trail_pullback"] if self._rl_params else TRAIL_PROFIT_PULLBACK
            sl_pts = self._rl_params["sl_pts"] if self._rl_params else DEFAULT_SL_PTS

            # TRAILING PROFIT: lock in gains (RL-chosen activation & pullback)
            if unrealized >= trail_act:
                if not self._trail_profit_active:
                    self._trail_profit_active = True
                    self._trail_profit_peak = unrealized
                    self._trail_profit_floor = unrealized * (1.0 - trail_pb)
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    direction = "LONG" if self.position == 1 else "SHORT"
                    print(f"[{now_str}] [{self.symbol} TRAIL-PROFIT] {direction} "
                          f"activated at ${unrealized:.0f} | floor=${self._trail_profit_floor:.0f} "
                          f"(RL: {self._rl_params['label'] if self._rl_params else 'default'})")
                elif unrealized > self._trail_profit_peak:
                    self._trail_profit_peak = unrealized
                    self._trail_profit_floor = unrealized * (1.0 - trail_pb)

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

            # DCA: add 1 contract (RL-chosen threshold, None = no DCA)
            dca_thresh = self._rl_params.get("dca_threshold") if self._rl_params else DCA_ADD_THRESHOLD
            if dca_thresh is not None and not self._dca_done and self.contracts_held < DCA_MAX_CONTRACTS:
                if unrealized <= dca_thresh:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} DCA-TRIGGER] unrealized ${unrealized:.0f} "
                          f"<= ${dca_thresh:.0f} (RL: {self._rl_params['label'] if self._rl_params else 'default'})")
                    if self._tp_limit_order_id:
                        actions.append(("cancel_tp_limit",))
                    actions.append(("dca_add", price))
                    return actions

            # SL check (RL-chosen distance)
            if self.position == 1 and price <= self.entry_price - sl_pts:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} SL-HIT] LONG @ {price:.2f} <= "
                      f"SL {self.entry_price - sl_pts:.2f} ({sl_pts}pts)")
                self._pending_rl = {"features": self.entry_features,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "SL"))
                return actions
            elif self.position == -1 and price >= self.entry_price + sl_pts:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} SL-HIT] SHORT @ {price:.2f} >= "
                      f"SL {self.entry_price + sl_pts:.2f} ({sl_pts}pts)")
                self._pending_rl = {"features": self.entry_features,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "SL"))
                return actions

        # Process new bricks — compute MFI on each
        if new_bricks:
            for brick in new_bricks:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                self._add_brick_data(brick["open"], brick["close"])
                mfi_signal = self._calc_mfi()
                mfi_str = f"MFI={self.mfi_value:.1f}" if self.mfi_value is not None else "MFI warming"
                print(f"[{now_str}] [{self.symbol} BRICK] {brick['direction'].upper()} "
                      f"{brick['open']:.2f} -> {brick['close']:.2f} "
                      f"(consecutive: {self.renko.consecutive_count()}) | {mfi_str}")

                # Opposite MFI signal = exit current position
                if self.position != 0 and mfi_signal:
                    should_exit_mfi = False
                    if self.position == 1 and mfi_signal == "overbought":
                        should_exit_mfi = True
                    elif self.position == -1 and mfi_signal == "oversold":
                        should_exit_mfi = True

                    if should_exit_mfi:
                        if self.position == 1:
                            cur_pnl = (price - self.entry_price) * self.pv * self.contracts_held
                        else:
                            cur_pnl = (self.entry_price - price) * self.pv * self.contracts_held
                        direction = "LONG" if self.position == 1 else "SHORT"
                        print(f"[{now_str}] [{self.symbol} MFI-EXIT] {direction} x{self.contracts_held} "
                              f"— opposite MFI {mfi_signal} (PnL ${cur_pnl:.0f})")
                        self._pending_rl = {"features": self.entry_features,
                                            "mae": self.trade_mae, "mfe": self.trade_mfe}
                        if self._tp_limit_order_id:
                            actions.append(("cancel_tp_limit",))
                        actions.append(("flatten", price, "MFI_REVERSAL"))
                        self._prev_brick_dir = brick["direction"]
                        return actions

                # MFI crossing — enter if MSS already confirmed the direction
                if mfi_signal and self.position == 0 and in_trade_session():
                    if mfi_signal == "oversold":
                        direction = "LONG"
                    else:
                        direction = "SHORT"
                    matches_mss = ((self._pending_mss == "bullish" and direction == "LONG") or
                                   (self._pending_mss == "bearish" and direction == "SHORT"))
                    if matches_mss:
                        features = self.extract_features()
                        should_enter, reason = self.ml.should_enter(features)
                        print(f"[{now_str}] [{self.symbol} MFI-CONFIRM] {mfi_signal.upper()} "
                              f"-> {direction} | MSS {self._pending_mss} already active | {reason}")
                        if should_enter:
                            self._pending_mss = None
                            self.mss.reset()
                            actions.append(("enter_short" if direction == "SHORT" else "enter_long", price))
                            self._prev_brick_dir = brick["direction"]
                            return actions
                    else:
                        print(f"[{now_str}] [{self.symbol} MFI-DOT] {mfi_signal.upper()} "
                              f"-> {direction} | no matching MSS pending")

                self._prev_brick_dir = brick["direction"]

        # Update MSS from Renko brick reversals
        self.mss.update_swings(self.renko.bricks)

        if self.position == 0 and in_trade_session():
            if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} DAILY-LIMIT] "
                      f"daily loss ${self.daily_loss:.0f} >= ${DAILY_LOSS_LIMIT:.0f} — no new entries")
                self._pending_mss = None
            else:
                mss_signal = self.mss.check(price)
                if mss_signal and mss_signal != self._pending_mss:
                    self._pending_mss = mss_signal
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    if mss_signal == "bearish":
                        print(f"[{now_str}] [{self.symbol} MSS-SHIFT] BEARISH "
                              f"@ {price:.2f} broke SL {self.mss.swing_lows[-1]:.2f} "
                              f"| waiting for MFI overbought -> SHORT")
                    else:
                        print(f"[{now_str}] [{self.symbol} MSS-SHIFT] BULLISH "
                              f"@ {price:.2f} broke SH {self.mss.swing_highs[-1]:.2f} "
                              f"| waiting for MFI oversold -> LONG")

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
            "rl_action_idx": self._rl_action_idx,
            "rl_params": self._rl_params,
            "tp_lock_pnl": self._tp_lock_pnl,
            "candle_current": self.candles._current,
            "candle_history": self.candles.candles[-20:],
            "bb_candle_current": self.bb_candles._current,
            "bb_candle_history": self.bb_candles.candles[-30:],
            "pending_mss": self._pending_mss,
            "brick_closes": self.brick_closes[-200:],
            "brick_opens": self.brick_opens[-200:],
            "brick_typicals": self.brick_typicals[-200:],
            "brick_volumes": self.brick_volumes[-200:],
            "mfi_value": self.mfi_value,
            "prev_mfi_value": self.prev_mfi_value,
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        age = time.time() - state.get("saved_at", 0)
        self.daily_loss = state.get("daily_loss", 0.0)
        self._prev_brick_dir = state.get("prev_brick_dir")
        self._dca_done = state.get("dca_done", False)
        self._entry_prices = state.get("entry_prices", [])
        self._rl_action_idx = state.get("rl_action_idx")
        self._rl_params = state.get("rl_params")
        self._tp_lock_pnl = state.get("tp_lock_pnl", 0.0)

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
        self._pending_mss = state.get("pending_mss")
        self.brick_closes = state.get("brick_closes", [])
        self.brick_opens = state.get("brick_opens", [])
        self.brick_typicals = state.get("brick_typicals", [])
        self.brick_volumes = state.get("brick_volumes", [])
        self.mfi_value = state.get("mfi_value")
        self.prev_mfi_value = state.get("prev_mfi_value")

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

class RenkoMFIMSSBot:
    def __init__(self, symbol_configs: list, tg_token: str = "",
                 tg_chat: str = "", tg_keys: list = None,
                 tp_webhooks: list = None, peer_dirs: list = None):
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tp_webhooks = tp_webhooks or []
        self.peer_dirs = peer_dirs or []
        self.running = True
        self.suite = None
        self.states = {}
        self.state_file = os.path.join(os.getcwd(), "bot_state_mfi_mss.json")
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

    def sync_from_peers(self):
        if not self.peer_dirs:
            return
        my_ts = 0.0
        for sym, st in self.states.items():
            try:
                with open(self.state_file, "r") as f:
                    own = json.load(f)
                if sym in own:
                    my_ts = own[sym].get("saved_at", 0.0)
            except Exception:
                pass
            break

        best_peer = None
        best_ts = my_ts
        for peer_dir in self.peer_dirs:
            peer_file = os.path.join(peer_dir, "bot_state_mfi_mss.json")
            if not os.path.exists(peer_file):
                continue
            try:
                with open(peer_file, "r") as f:
                    peer_state = json.load(f)
                for sym in self.states:
                    if sym in peer_state:
                        pts = peer_state[sym].get("saved_at", 0.0)
                        if pts > best_ts:
                            best_ts = pts
                            best_peer = (peer_dir, peer_state)
            except Exception:
                continue

        if best_peer is None:
            print("[PEER-SYNC] No peer has more recent state")
            return

        peer_dir, peer_state = best_peer
        age_diff = best_ts - my_ts
        print(f"[PEER-SYNC] Found newer state from {os.path.basename(peer_dir)} "
              f"({age_diff:.0f}s newer)")

        MARKET_KEYS = [
            "renko_last_close", "renko_last_dir", "renko_bricks", "prev_brick_dir",
            "brick_closes", "brick_opens", "brick_typicals", "brick_volumes",
            "mfi_value", "prev_mfi_value", "pending_mss",
            "candle_current", "candle_history", "bb_candle_current", "bb_candle_history",
        ]

        for sym, st in self.states.items():
            if sym not in peer_state:
                continue
            ps = peer_state[sym]
            renko_bricks = ps.get("renko_bricks", [])
            if renko_bricks:
                st.renko.bricks = renko_bricks
                st.renko._last_close = ps.get("renko_last_close")
                st.renko._last_direction = ps.get("renko_last_dir")
            st._prev_brick_dir = ps.get("prev_brick_dir", st._prev_brick_dir)
            st.brick_closes = ps.get("brick_closes", st.brick_closes)
            st.brick_opens = ps.get("brick_opens", st.brick_opens)
            st.brick_typicals = ps.get("brick_typicals", st.brick_typicals)
            st.brick_volumes = ps.get("brick_volumes", st.brick_volumes)
            st.mfi_value = ps.get("mfi_value", st.mfi_value)
            st.prev_mfi_value = ps.get("prev_mfi_value", st.prev_mfi_value)
            st._pending_mss = ps.get("pending_mss", st._pending_mss)
            cc = ps.get("candle_current")
            if cc:
                st.candles._current = cc
            ch = ps.get("candle_history", [])
            if ch:
                st.candles.candles = ch
            bcc = ps.get("bb_candle_current")
            if bcc:
                st.bb_candles._current = bcc
            bch = ps.get("bb_candle_history", [])
            if bch:
                st.bb_candles.candles = bch

            print(f"  [{sym}] Synced from peer: {len(renko_bricks)} bricks, "
                  f"MFI={round(st.mfi_value or 0, 1)}, pending_mss={st._pending_mss}")

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

                        rl_idx = st._rl_action_idx
                        st._rl_action_idx = None
                        st._rl_params = None
                        st._tp_lock_pnl = 0.0

                        async def _deferred_ml(st_ref, feat, pnl_before, m, mf, rl_i):
                            try:
                                await st_ref._sync_pnl_from_platform()
                                real_pnl = st_ref.live_pnl - pnl_before + pnl_est
                                st_ref.ml.record_trade(feat, real_pnl, source="platform_close",
                                                       entered=True, mae=m, mfe=mf)
                                if rl_i is not None:
                                    st_ref.rl.update(feat, rl_i, real_pnl)
                            except Exception:
                                st_ref.ml.record_trade(feat, pnl_est, source="platform_close_est",
                                                       entered=True, mae=m, mfe=mf)
                                if rl_i is not None:
                                    st_ref.rl.update(feat, rl_i, pnl_est)

                        pnl_before = st.live_pnl - pnl_est
                        try:
                            loop = asyncio.get_event_loop()
                            loop.create_task(_deferred_ml(st, features, pnl_before, mae, mfe, rl_idx))
                        except Exception:
                            st.ml.record_trade(features, pnl_est, source="platform_close_est",
                                               entered=True, mae=mae, mfe=mfe)
                            if rl_idx is not None:
                                st.rl.update(features, rl_idx, pnl_est)
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
        print(f"[BOT] Renko MFI + MSS Bot starting...")
        print(f"[BOT] Strategy: Renko {BRICK_SIZE}pt bricks, DCA reversal")
        print(f"[BOT] ENTRY: brick color flip -> enter 1 contract")
        print(f"[BOT] DCA: add 1 more at -${abs(DCA_ADD_THRESHOLD)}, max {DCA_MAX_CONTRACTS} contracts")
        print(f"[BOT] EXIT: TP=${DCA_TP_DOLLARS} total PnL / opposite brick color")
        print(f"[BOT] ML: logistic regression skips bad setups")
        print(f"[BOT] RL: Q-learning optimizes SL/trail per market state")
        print(f"[BOT] Session: {TRADE_SESSION_START.strftime('%H:%M')} - "
              f"{TRADE_SESSION_END.strftime('%H:%M')} ET")
        print(f"[BOT] Symbols: {symbols}")

        self.load_all_state()
        self.sync_from_peers()

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
        print(f"[BOT] Trading LIVE — Renko MFI + MSS strategy ({', '.join(symbols)})")
        print(f"[BOT] Watchdog: {WATCHDOG_TIMEOUT}s timeout")
        print(f"[BOT] Press Ctrl+C to stop")

        for sym, st in self.states.items():
            msg = (f"STATUS|Renko MFI+MSS Bot started (ML+RL)\n"
                   f"Account: {acct}\n"
                   f"Brick: {BRICK_SIZE}pt | TP: ${DCA_TP_DOLLARS} | SL/trail: RL-adaptive\n"
                   f"Mode: Brick flip entry, DCA, trail profit, TP lock\n"
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
    parser = argparse.ArgumentParser(description="TopstepX Renko MFI + MSS Bot")
    parser.add_argument("--symbols", default="NQ:1",
                        help="Multi-symbol: 'NQ:1:ntfy-topic,ES:1'")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--tick-interval", type=int, default=1)
    parser.add_argument("--tp-webhooks", default="", help="Comma-separated TradersPost URLs")
    parser.add_argument("--peer-dirs", default="", help="Comma-separated peer bot directories for state sync")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []
    tp_webhooks = [u.strip() for u in args.tp_webhooks.split(",") if u.strip()] if args.tp_webhooks else []
    peer_dirs = [d.strip() for d in args.peer_dirs.split(",") if d.strip()] if args.peer_dirs else []
    symbol_configs = parse_symbol_configs(args.symbols)

    if not symbol_configs:
        print("ERROR: No symbols configured")
        return

    print(f"[BOT] Renko MFI + MSS Bot (ML + RL)")
    print(f"[BOT] Brick: {BRICK_SIZE}pt | MFI({MFI_PERIOD}) OB={MFI_OVERBOUGHT}/OS={MFI_OVERSOLD}")
    print(f"[BOT] ENTRY: 15s MSS trend shift -> MFI exhaustion confirms -> enter")
    print(f"[BOT] EXIT: opposite MFI signal / TP hit / trail profit / SL (RL-chosen)")
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
        bot = RenkoMFIMSSBot(
            symbol_configs=symbol_configs,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            tp_webhooks=tp_webhooks,
            peer_dirs=peer_dirs,
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
                          f"STATUS|Renko MFI+MSS bot crashed, restarting in {retry_delay}s")
            retry_delay = min(retry_delay * 2, 300)
        finally:
            bot.save_all_state()
            loop.close()

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
