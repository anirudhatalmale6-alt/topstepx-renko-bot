"""
TopstepX StdDev EMA Circle Flip Bot (LIVE)
============================================
Strategy: Daily Standard Deviation levels + 9 EMA flip
- Computes daily StdDev from historical data, calculates price levels at
  0, ±0.5σ, ±1σ, ±1.5σ, ±2σ, ±2.5σ around the daily open (session open)
- 9 EMA on 5-minute candles built from 1-second ticks
- LONG:  EMA(9) crosses above the 0 level (daily open)
- SHORT: EMA(9) crosses below the 0 level
- EXIT:  Price touches 0.5σ (long TP) or -0.5σ (short TP)
- FLIP:  If in LONG and EMA crosses below 0 → flip to SHORT (vice versa)
- Progressive position sizing: 1x → 2x → 3x → 4x on consecutive flips
- RL (Q-learning): learns optimal entries/exits per StdDev zone

Usage:
    python stddev_ema_bot.py --symbols "NQ:1" --tick-interval 1
"""

import asyncio
import argparse
import gc
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

SESSION_START = dtime(18, 30, 0)   # 6:30 PM ET start
SESSION_END = dtime(16, 0, 0)      # 4:00 PM ET end
TRADING_DAYS = [0, 1, 2, 3, 4, 6]  # Mon-Fri + Sun
BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

# Trading session filter (from Pine Script: 9:27 AM - 4:00 PM ET)
TRADE_SESSION_START = dtime(9, 27, 0)
TRADE_SESSION_END = dtime(16, 0, 0)

# VWAP / candle settings
CANDLE_MINUTES = 15  # 15-minute candles for VWAP

# StdDev level multipliers
STDDEV_LEVELS = [0, 0.5, 1.0, 1.5, 2.0, 2.5]
STDDEV_LOOKBACK = 5000  # daily bars for StdDev calculation

# Position sizing (progressive)
BASE_SIZE = 1
MAX_SIZE = 4

# Risk management
MAX_LOSS_DOLLARS = 300.0
QUICK_EXIT_DOLLARS = 150.0
QUICK_EXIT_SECS = 30
DAILY_LOSS_LIMIT = 1000.0

# Point values and tick sizes
MINTICK_VALUES = {
    "NQ": 0.25, "ES": 0.25, "MNQ": 0.25, "MES": 0.25,
    "YM": 1.0, "RTY": 0.10,
}
POINT_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0,
    "YM": 5.0, "RTY": 10.0,
}

# Watchdog
WATCHDOG_TIMEOUT = 300


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
    return TRADE_SESSION_START <= t < TRADE_SESSION_END


# ============================================================
# StdDev Level Calculator
# ============================================================

class StdDevLevels:
    """Computes daily standard deviation levels from historical data."""

    def __init__(self, symbol: str, lookback: int = STDDEV_LOOKBACK):
        self.symbol = symbol
        self.lookback = lookback
        self.daily_stdev = None  # StdDev of (close-open)/open
        self.session_open = None  # Price at session open
        self.levels = {}  # level_name -> price  (e.g., "0.5" -> 29200.5)
        self._session_date = None  # Track which session day we computed for

    def compute_stdev_from_history(self):
        """Fetch historical daily data and compute standard deviation."""
        try:
            import yfinance as yf
            ticker_map = {"NQ": "NQ=F", "ES": "ES=F", "MNQ": "MNQ=F",
                          "MES": "MES=F", "YM": "YM=F", "RTY": "RTY=F"}
            yf_sym = ticker_map.get(self.symbol, f"{self.symbol}=F")
            df = yf.download(yf_sym, period="max", interval="1d", progress=False)
            if df.empty:
                print(f"[STDDEV] No historical data for {yf_sym}")
                return False
            if hasattr(df.columns, 'droplevel'):
                try:
                    df.columns = df.columns.droplevel(1)
                except Exception:
                    pass
            df = df.tail(self.lookback)
            daily_returns = (df["Close"] - df["Open"]) / df["Open"]
            self.daily_stdev = float(daily_returns.std())
            print(f"[STDDEV] Computed from {len(df)} daily bars: "
                  f"StdDev = {self.daily_stdev:.6f} ({self.daily_stdev * 100:.3f}%)")
            return True
        except Exception as e:
            print(f"[STDDEV] Error computing from history: {e}")
            return False

    def set_session_open(self, price: float):
        """Set the session open price and recalculate all levels."""
        self.session_open = price
        if self.daily_stdev is None:
            print(f"[STDDEV] WARNING: StdDev not computed, using default 1.5%")
            self.daily_stdev = 0.015
        stdev_pts = self.daily_stdev * price
        self.levels = {}
        for mult in STDDEV_LEVELS:
            self.levels[f"{mult}"] = price + mult * stdev_pts
            if mult != 0:
                self.levels[f"-{mult}"] = price - mult * stdev_pts
        lvl_str = " | ".join(f"{k}: {v:.2f}" for k, v in sorted(self.levels.items(), key=lambda x: float(x[0])))
        print(f"[STDDEV] Session open: {price:.2f}, StdDev: {stdev_pts:.2f} pts")
        print(f"[STDDEV] Levels: {lvl_str}")

    def get_level(self, name: str) -> float:
        return self.levels.get(name, None)

    def get_zone(self, price: float) -> str:
        """Return which StdDev zone the price is in (e.g., '0_to_0.5', 'above_2.5')."""
        if self.session_open is None or self.daily_stdev is None:
            return "unknown"
        stdev_pts = self.daily_stdev * self.session_open
        if stdev_pts <= 0:
            return "unknown"
        sigma = (price - self.session_open) / stdev_pts
        if sigma > 2.5:
            return "above_2.5"
        elif sigma > 2.0:
            return "2.0_to_2.5"
        elif sigma > 1.5:
            return "1.5_to_2.0"
        elif sigma > 1.0:
            return "1.0_to_1.5"
        elif sigma > 0.5:
            return "0.5_to_1.0"
        elif sigma > 0:
            return "0_to_0.5"
        elif sigma > -0.5:
            return "-0.5_to_0"
        elif sigma > -1.0:
            return "-1.0_to_-0.5"
        elif sigma > -1.5:
            return "-1.5_to_-1.0"
        elif sigma > -2.0:
            return "-2.0_to_-1.5"
        elif sigma > -2.5:
            return "-2.5_to_-2.0"
        else:
            return "below_-2.5"

    def check_session_reset(self) -> bool:
        """Returns True if we should reset for a new session (new trading day)."""
        now_et = datetime.now(ET)
        session_day = now_et.date() if now_et.time() >= SESSION_START else now_et.date() - timedelta(days=1)
        if self._session_date != session_day:
            self._session_date = session_day
            self.session_open = None
            self.levels = {}
            print(f"[STDDEV] New session day {session_day} — levels reset")
            return True
        return False


# ============================================================
# 15-Minute Candle Builder + Session VWAP
# ============================================================

class CandleBuilder:
    """Builds fixed-interval candles from tick data with session VWAP."""

    def __init__(self, interval_minutes: int = CANDLE_MINUTES):
        self.interval = interval_minutes
        self.candles = []
        self._current = None
        self._max_candles = 500
        # Session VWAP
        self.vwap = None
        self.vwap_cum_pv = 0.0
        self.vwap_cum_vol = 0
        self._vwap_session_date = None

    def _candle_start(self, ts: float) -> float:
        dt = datetime.fromtimestamp(ts, tz=ET)
        minute_of_day = dt.hour * 60 + dt.minute
        candle_minute = (minute_of_day // self.interval) * self.interval
        return dt.replace(hour=candle_minute // 60, minute=candle_minute % 60,
                          second=0, microsecond=0).timestamp()

    def _check_vwap_reset(self):
        today = datetime.now(ET).date()
        if self._vwap_session_date != today:
            self.vwap_cum_pv = 0.0
            self.vwap_cum_vol = 0
            self.vwap = None
            self._vwap_session_date = today

    def feed(self, price: float, ts: float = None) -> dict:
        if ts is None:
            ts = time.time()
        self._check_vwap_reset()
        candle_start = self._candle_start(ts)

        if self._current is None:
            self._current = {"start": candle_start, "open": price, "high": price,
                             "low": price, "close": price, "volume": 1}
            return None

        if candle_start > self._current["start"]:
            completed = dict(self._current)
            # Update VWAP with completed candle
            typical = (completed["high"] + completed["low"] + completed["close"]) / 3.0
            vol = completed["volume"]
            self.vwap_cum_pv += typical * vol
            self.vwap_cum_vol += vol
            if self.vwap_cum_vol > 0:
                self.vwap = self.vwap_cum_pv / self.vwap_cum_vol
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

    def get_closes(self, n: int = None) -> list:
        closes = [c["close"] for c in self.candles]
        if self._current:
            closes.append(self._current["close"])
        if n is not None:
            return closes[-n:]
        return closes

    def get_momentum(self, lookback: int = 3) -> float:
        """Price momentum over last N candles (pts per candle)."""
        closes = [c["close"] for c in self.candles]
        if len(closes) < lookback + 1:
            return 0.0
        return (closes[-1] - closes[-(lookback + 1)]) / lookback


# ============================================================
# Reinforcement Learning (Q-Learning)
# ============================================================

RL_WARMUP_TRADES = 20
RL_ALPHA = 0.15
RL_ALPHA_REGIME = 0.30
RL_GAMMA = 0.95
RL_EPSILON_START = 0.90
RL_EPSILON_MIN = 0.10
RL_EPSILON_DECAY = 0.985
RL_PNL_SCALE = 500.0
RL_Q_DECAY = 0.998
RL_REGIME_WINDOW = 15
RL_REGIME_THRESHOLD = 0.20
EARLY_EXIT_MULTIPLIER = 1.5
EARLY_EXIT_MIN_SAMPLES = 3
EARLY_EXIT_COOLDOWN = 15


class TradeML:
    """Q-learning RL filter for StdDev EMA strategy."""

    ACTION_ENTER = 0
    ACTION_SKIP = 1
    ACTION_WAIT = 2
    ACTION_FLIP = 3

    def __init__(self, data_file: str):
        self.data_file = data_file
        self.q_table = {}
        self.trades = []
        self.epsilon = RL_EPSILON_START
        self.total_trades = 0
        self.recent_outcomes = []
        self.regime_shift = False
        self.state_mae = {}
        self.state_mfe = {}
        self.state_post_exit = {}
        self.state_recovery = {}
        self._load()

    def _state_key(self, features: dict) -> str:
        """Discretize: zone|vwap_pos|price_vs_vwap|time_zone|momentum|streak"""
        zone = features.get("zone", "unknown")
        vwap_pos = features.get("vwap_position", "unknown")

        price_vs_vwap = features.get("price_vs_vwap", 0.0)
        if price_vs_vwap > 20:
            pvw = "far_above"
        elif price_vs_vwap > 5:
            pvw = "above"
        elif price_vs_vwap < -20:
            pvw = "far_below"
        elif price_vs_vwap < -5:
            pvw = "below"
        else:
            pvw = "near"

        hour = features.get("hour", 12)
        if hour < 10:
            time_zone = "early"
        elif hour < 12:
            time_zone = "morning"
        elif hour < 14:
            time_zone = "midday"
        else:
            time_zone = "afternoon"

        momentum = features.get("momentum", "flat")

        recent_wins = sum(1 for p in self.recent_outcomes[-5:] if p > 0)
        recent_total = min(len(self.recent_outcomes), 5)
        if recent_total < 3:
            streak = "new"
        elif recent_wins >= 4:
            streak = "hot"
        elif recent_wins <= 1:
            streak = "cold"
        else:
            streak = "mixed"

        return f"{zone}|{vwap_pos}|{pvw}|{time_zone}|{momentum}|{streak}"

    def _get_q(self, state_key: str) -> list:
        if state_key not in self.q_table:
            self.q_table[state_key] = [0.0, 0.0, 0.0, 0.0]
        q = self.q_table[state_key]
        while len(q) < 4:
            q.append(0.0)
        self.q_table[state_key] = q
        return q

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r") as f:
                    data = json.load(f)
                self.q_table = data.get("q_table", {})
                self.trades = data.get("trades", [])
                self.epsilon = data.get("epsilon", RL_EPSILON_START)
                self.total_trades = data.get("total_trades", len(self.trades))
                self.recent_outcomes = data.get("recent_outcomes", [])
                self.regime_shift = data.get("regime_shift", False)
                self.state_mae = data.get("state_mae", {})
                self.state_mfe = data.get("state_mfe", {})
                self.state_post_exit = data.get("state_post_exit", {})
                self.state_recovery = data.get("state_recovery", {})
                print(f"[RL] Loaded: {self.total_trades} trades, {len(self.q_table)} states, "
                      f"epsilon={self.epsilon:.3f}")
            except Exception as e:
                print(f"[RL] Load error: {e}")

    def _save(self):
        try:
            trimmed_mae = {}
            for k, v in self.state_mae.items():
                trimmed_mae[k] = {
                    "win_mae": v.get("win_mae", [])[-50:],
                    "lose_mae": v.get("lose_mae", [])[-50:],
                }
            data = {
                "q_table": self.q_table,
                "trades": self.trades[-500:],
                "epsilon": self.epsilon,
                "total_trades": self.total_trades,
                "recent_outcomes": self.recent_outcomes[-20:],
                "regime_shift": self.regime_shift,
                "state_mae": trimmed_mae,
                "state_mfe": {k: v[-50:] for k, v in self.state_mfe.items()},
                "state_post_exit": {k: v[-50:] for k, v in self.state_post_exit.items()},
                "state_recovery": self.state_recovery,
            }
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.data_file)
        except Exception as e:
            print(f"[RL] Save error: {e}")

    def extract_features(self, zone: str, vwap_position: str,
                         price_vs_vwap: float, momentum_pts: float,
                         price: float, vwap: float,
                         candle_closes: list) -> dict:
        hour = datetime.now(ET).hour
        if momentum_pts > 15:
            momentum = "strong_up"
        elif momentum_pts > 5:
            momentum = "up"
        elif momentum_pts < -15:
            momentum = "strong_down"
        elif momentum_pts < -5:
            momentum = "down"
        else:
            momentum = "flat"

        vol = 0.0
        if len(candle_closes) >= 10:
            vol = float(np.std(candle_closes[-10:]))

        return {
            "zone": zone,
            "vwap_position": vwap_position,
            "price_vs_vwap": round(price_vs_vwap, 2),
            "hour": hour,
            "momentum": momentum,
            "price": round(price, 2),
            "vwap": round(vwap, 2) if vwap else None,
            "momentum_pts": round(momentum_pts, 2),
            "volatility": round(vol, 4),
        }

    def should_skip(self, features: dict) -> tuple:
        if self.total_trades < RL_WARMUP_TRADES:
            return "enter", 0.0, f"RL warmup ({self.total_trades}/{RL_WARMUP_TRADES})"
        state = self._state_key(features)
        q_vals = self._get_q(state)
        q_enter, q_skip, q_wait, q_flip = q_vals

        if random.random() < self.epsilon:
            action = random.choice([self.ACTION_ENTER, self.ACTION_SKIP,
                                    self.ACTION_WAIT, self.ACTION_FLIP])
            choice = "explore"
        else:
            best = max(q_enter, q_skip, q_wait, q_flip)
            if best == q_enter:
                action = self.ACTION_ENTER
            elif best == q_flip:
                action = self.ACTION_FLIP
            elif best == q_wait:
                action = self.ACTION_WAIT
            else:
                action = self.ACTION_SKIP
            choice = "exploit"

        action_names = {self.ACTION_ENTER: "enter", self.ACTION_SKIP: "skip",
                        self.ACTION_WAIT: "wait", self.ACTION_FLIP: "flip"}
        action_str = action_names[action]
        reason = (f"RL|{state}|Q(now)={q_enter:+.2f} Q(skip)={q_skip:+.2f} "
                  f"Q(wait)={q_wait:+.2f} Q(flip)={q_flip:+.2f}|eps={self.epsilon:.2f}|"
                  f"{choice}|{action_str}")
        return action_str, 0.0, reason

    def _detect_regime_shift(self) -> bool:
        total = len(self.trades)
        if total < RL_REGIME_WINDOW + 10:
            return False
        overall_wr = sum(1 for t in self.trades if t["win"] == 1) / total
        recent = self.trades[-RL_REGIME_WINDOW:]
        recent_wr = sum(1 for t in recent if t["win"] == 1) / len(recent)
        return (overall_wr - recent_wr) >= RL_REGIME_THRESHOLD

    def _decay_q_table(self):
        for key in self.q_table:
            for i in range(len(self.q_table[key])):
                self.q_table[key][i] *= RL_Q_DECAY

    def record_trade(self, features: dict, pnl: float, source: str = "live",
                     entry_action: str = "enter", mae: float = 0.0, mfe: float = 0.0):
        if features is None:
            return
        state = self._state_key(features)
        reward = pnl / RL_PNL_SCALE

        if state not in self.state_mae:
            self.state_mae[state] = {"win_mae": [], "lose_mae": []}
        bucket = "win_mae" if pnl > 0 else "lose_mae"
        self.state_mae[state][bucket].append(mae)
        if len(self.state_mae[state][bucket]) > 50:
            self.state_mae[state][bucket] = self.state_mae[state][bucket][-50:]

        if mfe > 0:
            if state not in self.state_mfe:
                self.state_mfe[state] = []
            self.state_mfe[state].append(mfe)
            if len(self.state_mfe[state]) > 50:
                self.state_mfe[state] = self.state_mfe[state][-50:]

        if mae < -30:
            if state not in self.state_recovery:
                self.state_recovery[state] = {"dip_then_tp": 0, "dip_then_fail": 0}
            if pnl > 0:
                self.state_recovery[state]["dip_then_tp"] += 1
            else:
                self.state_recovery[state]["dip_then_fail"] += 1

        self.regime_shift = self._detect_regime_shift()
        alpha = RL_ALPHA_REGIME if self.regime_shift else RL_ALPHA
        self._decay_q_table()

        q_vals = self._get_q(state)
        action_map = {"enter": self.ACTION_ENTER, "wait": self.ACTION_WAIT,
                       "flip": self.ACTION_FLIP}
        action_idx = action_map.get(entry_action, self.ACTION_ENTER)
        old_q = q_vals[action_idx]
        q_vals[action_idx] = old_q + alpha * (reward - old_q)

        if pnl < 0:
            old_skip_q = q_vals[self.ACTION_SKIP]
            skip_reward = abs(reward) * 0.3
            q_vals[self.ACTION_SKIP] = old_skip_q + alpha * (skip_reward - old_skip_q)

        if entry_action == "flip" and pnl > 0:
            q_vals[self.ACTION_ENTER] += alpha * (-abs(reward) * 0.2)
        elif entry_action == "enter" and pnl > 0:
            q_vals[self.ACTION_FLIP] += alpha * (-abs(reward) * 0.1)

        self.recent_outcomes.append(pnl)
        if len(self.recent_outcomes) > 20:
            self.recent_outcomes = self.recent_outcomes[-20:]

        trade = {
            "features": features, "state": state, "pnl": pnl,
            "win": 1 if pnl > 0 else 0, "entry_action": entry_action,
            "q_after": list(q_vals), "alpha_used": alpha,
            "regime_shift": self.regime_shift, "epsilon": self.epsilon,
            "source": source, "timestamp": datetime.now(ET).isoformat(),
        }
        self.trades.append(trade)
        self.total_trades += 1

        if self.regime_shift:
            self.epsilon = min(0.50, self.epsilon + 0.05)
        else:
            self.epsilon = max(RL_EPSILON_MIN, self.epsilon * RL_EPSILON_DECAY)
        self._save()

        wins = sum(1 for t in self.trades if t["win"] == 1)
        total = len(self.trades)
        print(f"[RL] Trade recorded: PnL=${pnl:.2f} | {entry_action} | state={state} | "
              f"Q={[f'{v:+.3f}' for v in q_vals]} | eps={self.epsilon:.3f} | "
              f"{total} trades, {wins} wins ({100*wins/total:.0f}%)")

    def stats(self) -> str:
        if not self.trades:
            return "No trades recorded"
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"] == 1)
        total_pnl = sum(t["pnl"] for t in self.trades)
        return (f"RL: {total} trades | W:{wins} L:{total-wins} | "
                f"Win%: {100*wins/total:.0f}% | PnL: ${total_pnl:.2f} | "
                f"{len(self.q_table)} states | eps={self.epsilon:.3f}")

    def should_early_exit(self, features: dict, current_mae: float) -> tuple:
        if features is None:
            return False, "", 0.0
        state = self._state_key(features)
        mae_data = self.state_mae.get(state)
        if not mae_data:
            return False, "", 0.0
        win_maes = mae_data.get("win_mae", [])
        if len(win_maes) < EARLY_EXIT_MIN_SAMPLES:
            return False, "", 0.0
        avg_win_mae = sum(win_maes) / len(win_maes)
        threshold = avg_win_mae * EARLY_EXIT_MULTIPLIER
        if current_mae < threshold:
            return True, (f"MAE ${current_mae:.0f} < threshold ${threshold:.0f} "
                          f"(avg winner MAE ${avg_win_mae:.0f} x {EARLY_EXIT_MULTIPLIER})"), avg_win_mae
        return False, "", avg_win_mae


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

        # Market data
        self.stddev = StdDevLevels(symbol)
        self.candles = CandleBuilder(CANDLE_MINUTES)
        self.last_price = 0.0
        self.last_tick_time = 0.0

        # Position state
        self.position = 0       # 0=flat, 1=long, -1=short
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_features = None
        self.entry_action = "enter"
        self.live_pnl = 0.0
        self.trade_mae = 0.0
        self.trade_mfe = 0.0
        self.mae_history = []

        # Flip state
        self.flip_count = 0
        self.tp_hit_this_session = False
        self.prev_zone = None  # Track zone changes

        # RL
        self.ml = TradeML(os.path.join(os.getcwd(), f"rl_state_{symbol}_stddev.json"))

        # TopstepX context
        self.ctx = None
        self._suite_client = None
        self._pending_rl = None
        self._start_balance = None
        self._pnl_session_day = None

        # Daily loss tracking
        self.daily_loss = 0.0

        # State save counter
        self._save_counter = 0

    def get_position_size(self) -> int:
        """Progressive sizing: 1x, 2x, 3x, 4x based on flip count."""
        if self.flip_count <= 1:
            size = self.base_qty * 1
        elif self.flip_count == 2:
            size = self.base_qty * 2
        elif self.flip_count == 3:
            size = self.base_qty * 3
        else:
            size = self.base_qty * 4
        return min(size, self.base_qty * MAX_SIZE)

    def get_vwap_position(self) -> str:
        """Returns VWAP position relative to StdDev levels."""
        vwap = self.candles.vwap
        if vwap is None:
            return "unknown"
        l05 = self.stddev.get_level("0.5")
        l0 = self.stddev.get_level("0")
        lm05 = self.stddev.get_level("-0.5")
        if l05 is None or l0 is None or lm05 is None:
            return "unknown"
        if vwap > l05:
            return "above_0.5"
        elif vwap > l0:
            return "0_to_0.5"
        elif vwap > lm05:
            return "-0.5_to_0"
        else:
            return "below_-0.5"

    def get_price_vs_vwap(self) -> float:
        """Returns price distance from VWAP in points."""
        vwap = self.candles.vwap
        if vwap is None:
            return 0.0
        return self.last_price - vwap

    def extract_features(self) -> dict:
        zone = self.stddev.get_zone(self.last_price)
        vwap_pos = self.get_vwap_position()
        pvw = self.get_price_vs_vwap()
        mom = self.candles.get_momentum(3)
        closes = self.candles.get_closes(20)
        return self.ml.extract_features(
            zone=zone, vwap_position=vwap_pos, price_vs_vwap=pvw,
            momentum_pts=mom, price=self.last_price,
            vwap=self.candles.vwap, candle_closes=closes)

    def check_tp_touch(self) -> int:
        """Check if price touches TP level.
        Returns: 1 = long TP (touched 0.5σ), -1 = short TP (touched -0.5σ), 0 = no touch."""
        if self.position == 1:
            tp_level = self.stddev.get_level("0.5")
            if tp_level is not None and self.last_price >= tp_level:
                return 1
        elif self.position == -1:
            tp_level = self.stddev.get_level("-0.5")
            if tp_level is not None and self.last_price <= tp_level:
                return -1
        return 0

    def check_zone_change(self) -> bool:
        """Returns True if price moved to a different StdDev zone."""
        curr_zone = self.stddev.get_zone(self.last_price)
        if curr_zone != self.prev_zone:
            self.prev_zone = curr_zone
            return True
        return False

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
                                  f"Platform: ${real_pnl:.2f} (drift ${drift:.2f}, "
                                  f"balance ${a.balance:,.2f})")
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
            print(f"[{self.symbol}] BLOCKED LONG: already in position")
            return False
        await self._ensure_flat_before_entry()
        qty = self.get_position_size()
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG @ {price:.2f} | "
              f"Flip #{self.flip_count} | Size: {qty} | Session P&L: ${self.live_pnl:.2f}")

        async def _do_order():
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
                    self.entry_time = time.time()
                    self.trade_mae = 0.0
                    self.trade_mfe = 0.0
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "LONG", self.symbol, price, qty),
                        kwargs={"ntfy_topic": self.ntfy_topic,
                                "tp_webhooks": self.tp_webhooks}, daemon=True).start()
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

        return await _do_order()

    async def _enter_short(self, price: float):
        if self.position != 0:
            print(f"[{self.symbol}] BLOCKED SHORT: already in position")
            return False
        await self._ensure_flat_before_entry()
        qty = self.get_position_size()
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT @ {price:.2f} | "
              f"Flip #{self.flip_count} | Size: {qty} | Session P&L: ${self.live_pnl:.2f}")

        async def _do_order():
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
                    self.entry_time = time.time()
                    self.trade_mae = 0.0
                    self.trade_mfe = 0.0
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "SHORT", self.symbol, price, qty),
                        kwargs={"ntfy_topic": self.ntfy_topic,
                                "tp_webhooks": self.tp_webhooks}, daemon=True).start()
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

        return await _do_order()

    async def _flatten(self, price: float, reason: str = "signal"):
        if self.position == 0:
            return
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl_before = self.live_pnl

        try:
            response = await asyncio.wait_for(
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

        old_pos = self.position
        self.position = 0
        self.contracts_held = 0

        # Send flat signal
        threading.Thread(target=send_signals, args=(
            self.tg_token, self.tg_chat, self.tg_keys,
            "FLAT", self.symbol, price, 0),
            kwargs={"ntfy_topic": self.ntfy_topic,
                    "tp_webhooks": self.tp_webhooks}, daemon=True).start()

        # Sync PnL from platform
        await self._sync_pnl_from_platform()
        real_trade_pnl = self.live_pnl - pnl_before
        if abs(real_trade_pnl - trade_pnl) > 2.0:
            print(f"[{self.symbol}] PnL correction: estimated ${trade_pnl:.2f} -> "
                  f"actual ${real_trade_pnl:.2f}")
            trade_pnl = real_trade_pnl

        # Record to RL
        if self._pending_rl:
            rl = self._pending_rl
            self.ml.record_trade(rl["features"], trade_pnl, source="live",
                                 entry_action=rl.get("entry_action", "enter"),
                                 mae=rl.get("mae", self.trade_mae),
                                 mfe=rl.get("mfe", self.trade_mfe))
            self._pending_rl = None
        elif self.entry_features:
            self.ml.record_trade(self.entry_features, trade_pnl, source="live",
                                 entry_action=self.entry_action,
                                 mae=self.trade_mae, mfe=self.trade_mfe)

        self.entry_features = None

    def tick(self, price: float, ts: float = None):
        """Process a new tick. RL-driven: evaluates state on each 15-min candle
        close and decides entry/exit. Levels provide context, RL decides action."""
        if ts is None:
            ts = time.time()
        self.last_price = price
        self.last_tick_time = ts
        actions = []

        # Session boundary reset
        now_et = datetime.now(ET)
        session_day = now_et.date() if now_et.time() >= SESSION_START else now_et.date() - timedelta(days=1)
        if self._pnl_session_day is None or self._pnl_session_day != session_day:
            self._pnl_session_day = session_day
            self._start_balance = None
            self.live_pnl = 0.0
            self.daily_loss = 0.0
            self.tp_hit_this_session = False
            self.flip_count = 0
            self.prev_zone = None
            print(f"[{self.symbol} SESSION] New session day {session_day} — all reset")

        if self.stddev.daily_stdev is None:
            self.stddev.compute_stdev_from_history()

        self.stddev.check_session_reset()
        if self.stddev.session_open is None and price > 0:
            self.stddev.set_session_open(price)

        completed_candle = self.candles.feed(price, ts)

        # Track MAE/MFE for open position
        if self.position != 0:
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.pv * self.contracts_held
            else:
                unrealized = (self.entry_price - price) * self.pv * self.contracts_held
            self.trade_mfe = max(self.trade_mfe, unrealized)
            self.trade_mae = min(self.trade_mae, unrealized)

            elapsed = ts - self.entry_time

            # Quick exit
            if elapsed <= QUICK_EXIT_SECS and unrealized < -QUICK_EXIT_DOLLARS:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} QUICK-EXIT] "
                      f"{'LONG' if self.position == 1 else 'SHORT'} ${unrealized:.0f} in {elapsed:.0f}s")
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                actions.append(("flatten", price, "QUICK_EXIT"))
                return actions

            # Max loss exit
            if unrealized < -MAX_LOSS_DOLLARS:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} MAX-LOSS] ${unrealized:.0f}")
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                actions.append(("flatten", price, "MAX_LOSS"))
                return actions

            # RL early exit
            if elapsed > EARLY_EXIT_COOLDOWN:
                should_exit, reason, _ = self.ml.should_early_exit(
                    self.entry_features, unrealized)
                if should_exit:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} EARLY-EXIT] {reason}")
                    self._pending_rl = {"features": self.entry_features,
                                        "entry_action": self.entry_action,
                                        "mae": self.trade_mae, "mfe": self.trade_mfe}
                    actions.append(("flatten", price, "EARLY_EXIT"))
                    return actions

            # TP check on every tick (price touching ±0.5σ)
            tp = self.check_tp_touch()
            if tp != 0:
                tp_level = "0.5" if tp == 1 else "-0.5"
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol}] TP HIT at {tp_level}σ | "
                      f"{'LONG' if self.position == 1 else 'SHORT'}")
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                self.tp_hit_this_session = True
                self.flip_count = 0
                actions.append(("flatten", price, f"TP_{tp_level}"))
                return actions

        # --- RL evaluation on 15-min candle close ---
        if completed_candle is None:
            return actions

        if not in_trade_session():
            return actions

        if self.tp_hit_this_session:
            return actions

        if self.candles.vwap is None:
            return actions

        now_str = datetime.now(ET).strftime("%H:%M:%S")
        zone = self.stddev.get_zone(price)
        vwap = self.candles.vwap
        pvw = self.get_price_vs_vwap()
        features = self.extract_features()

        # RL decides on every 15-min candle close
        if self.position == 0:
            # FLAT: RL decides whether to enter long, short, or skip
            action_str, _, reason = self.ml.should_skip(features)

            # Determine bias from market context:
            # Price above VWAP + positive zone = bullish bias
            # Price below VWAP + negative zone = bearish bias
            if pvw > 0 and zone in ("0_to_0.5", "0.5_to_1.0", "1.0_to_1.5"):
                bias = "long"
            elif pvw < 0 and zone in ("-0.5_to_0", "-1.0_to_-0.5", "-1.5_to_-1.0"):
                bias = "short"
            elif pvw > 10:
                bias = "long"
            elif pvw < -10:
                bias = "short"
            else:
                bias = "neutral"

            print(f"[{now_str}] [{self.symbol} EVAL] Zone: {zone} | VWAP: {vwap:.2f} | "
                  f"PvsVWAP: {pvw:+.1f} | Bias: {bias} | {reason}")

            if action_str == "enter":
                if bias == "long":
                    self.entry_features = features
                    self.entry_action = "enter"
                    self.flip_count = 1
                    actions.append(("enter_long", price))
                elif bias == "short":
                    self.entry_features = features
                    self.entry_action = "enter"
                    self.flip_count = 1
                    actions.append(("enter_short", price))
                # If neutral, don't enter even if RL says enter
            elif action_str == "flip":
                # RL says flip = enter opposite of bias
                if bias == "long":
                    self.entry_features = features
                    self.entry_action = "flip"
                    self.flip_count = 1
                    actions.append(("enter_short", price))
                elif bias == "short":
                    self.entry_features = features
                    self.entry_action = "flip"
                    self.flip_count = 1
                    actions.append(("enter_long", price))
            # skip/wait: do nothing

        else:
            # IN POSITION: RL evaluates whether to hold, close, or flip
            direction = "LONG" if self.position == 1 else "SHORT"
            action_str, _, reason = self.ml.should_skip(features)

            # Check if market context has shifted against position
            position_aligned = (self.position == 1 and pvw > 0) or \
                               (self.position == -1 and pvw < 0)

            print(f"[{now_str}] [{self.symbol} HOLD-CHECK] {direction} | Zone: {zone} | "
                  f"VWAP: {vwap:.2f} | PvsVWAP: {pvw:+.1f} | Aligned: {position_aligned} | {reason}")

            if action_str == "skip" and not position_aligned:
                # RL says skip + misaligned = close position
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                self.flip_count = 0
                actions.append(("flatten", price, "RL_CLOSE"))

            elif action_str == "flip":
                # Flip position
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                self.flip_count += 1
                self.entry_features = features
                self.entry_action = "flip"
                if self.position == 1:
                    actions.append(("flatten", price, "FLIP_TO_SHORT"))
                    actions.append(("enter_short", price))
                else:
                    actions.append(("flatten", price, "FLIP_TO_LONG"))
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
            "entry_action": self.entry_action,
            "live_pnl": self.live_pnl,
            "trade_mae": self.trade_mae,
            "trade_mfe": self.trade_mfe,
            "mae_history": self.mae_history[-100:],
            "flip_count": self.flip_count,
            "tp_hit_this_session": self.tp_hit_this_session,
            "daily_loss": self.daily_loss,
            "session_open": self.stddev.session_open,
            "daily_stdev": self.stddev.daily_stdev,
            "candle_data": self.candles.candles[-100:],
            "vwap": self.candles.vwap,
            "vwap_cum_pv": self.candles.vwap_cum_pv,
            "vwap_cum_vol": self.candles.vwap_cum_vol,
            "prev_zone": self.prev_zone,
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        age = time.time() - state.get("saved_at", 0)
        self.stddev.daily_stdev = state.get("daily_stdev")
        session_open = state.get("session_open")
        if session_open:
            self.stddev.set_session_open(session_open)
        candle_data = state.get("candle_data", [])
        self.candles.candles = candle_data
        self.candles.vwap = state.get("vwap")
        self.candles.vwap_cum_pv = state.get("vwap_cum_pv", 0.0)
        self.candles.vwap_cum_vol = state.get("vwap_cum_vol", 0)
        self.prev_zone = state.get("prev_zone")
        self.daily_loss = state.get("daily_loss", 0.0)

        if age < position_ttl:
            self.position = state.get("position", 0)
            self.contracts_held = state.get("contracts_held", 0)
            self.entry_price = state.get("entry_price", 0.0)
            self.entry_time = state.get("entry_time", 0.0)
            self.entry_features = state.get("entry_features")
            self.entry_action = state.get("entry_action", "enter")
            self.live_pnl = state.get("live_pnl", 0.0)
            self.trade_mae = state.get("trade_mae", 0.0)
            self.trade_mfe = state.get("trade_mfe", 0.0)
            self.mae_history = state.get("mae_history", [])
            self.flip_count = state.get("flip_count", 0)
            self.tp_hit_this_session = state.get("tp_hit_this_session", False)
            vwap_str = f"VWAP={self.candles.vwap:.2f}" if self.candles.vwap else "no VWAP yet"
            print(f"  [{self.symbol}] Restored: pos={self.position}, {vwap_str}")
            return True
        else:
            print(f"  [{self.symbol}] State too old ({age:.0f}s), position cleared")
            return False


# ============================================================
# Main Bot
# ============================================================

class StdDevBot:
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
        self.state_file = os.path.join(os.getcwd(), "bot_state_stddev.json")
        self.last_tick_time = time.time()

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
                        entry_action = st.entry_action
                        mae = st.trade_mae
                        mfe = st.trade_mfe

                        st.live_pnl += pnl_est
                        st.position = 0
                        st.contracts_held = 0
                        print(f"[WS] Platform closed {direction} {sym} — est PnL: ${pnl_est:.2f}")

                        threading.Thread(target=send_signals, args=(
                            self.tg_token, self.tg_chat, self.tg_keys,
                            "FLAT", sym, price, 0),
                            kwargs={"ntfy_topic": st.ntfy_topic,
                                    "tp_webhooks": st.tp_webhooks}, daemon=True).start()

                        async def _deferred_rl(st_ref, feat, pnl_before, ea, m, mf):
                            try:
                                await st_ref._sync_pnl_from_platform()
                                real_pnl = st_ref.live_pnl - pnl_before + pnl_est
                                st_ref.ml.record_trade(feat, real_pnl, source="platform_close",
                                                       entry_action=ea, mae=m, mfe=mf)
                            except Exception as e2:
                                st_ref.ml.record_trade(feat, pnl_est, source="platform_close_est",
                                                       entry_action=ea, mae=m, mfe=mf)

                        pnl_before = st.live_pnl - pnl_est
                        try:
                            loop = asyncio.get_event_loop()
                            loop.create_task(_deferred_rl(st, features, pnl_before,
                                                          entry_action, mae, mfe))
                        except Exception:
                            st.ml.record_trade(features, pnl_est, source="platform_close_est",
                                               entry_action=entry_action, mae=mae, mfe=mfe)
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
            print(f"[AUTO-DETECT] Switching: {configured} → {new_acct.name}")
            os.environ["PROJECT_X_ACCOUNT_NAME"] = new_acct.name
            send_telegram(self.tg_token, self.tg_chat,
                          f"STATUS|Account switched to {new_acct.name} (liquidation detected)")
            await self.suite.disconnect()
            from project_x_py import TradingSuite
            self.suite = await TradingSuite.create(
                instruments=symbols, timeframes=["1sec", "15min"], initial_days=1)
            self._register_websocket_handlers()
            for sym, st in self.states.items():
                st.ctx = self.suite[sym]
                st._suite_client = self.suite.client
        except Exception as e:
            print(f"[AUTO-DETECT] Error: {e}")

    async def run(self):
        from project_x_py import TradingSuite

        symbols = self._symbols_list()
        print(f"[BOT] StdDev EMA Flip Bot starting...")
        print(f"[BOT] Strategy: RL-driven with {CANDLE_MINUTES}min VWAP + StdDev zones, "
              f"TP at ±0.5σ")
        print(f"[BOT] Candle interval: {CANDLE_MINUTES}min (VWAP)")
        print(f"[BOT] Session: {TRADE_SESSION_START.strftime('%H:%M')} - "
              f"{TRADE_SESSION_END.strftime('%H:%M')} ET")
        print(f"[BOT] Symbols: {symbols}")

        self.load_all_state()

        self.suite = await TradingSuite.create(
            instruments=symbols, timeframes=["1sec", "15min"], initial_days=1)
        self._register_websocket_handlers()

        print(f"[BOT] Connected to TopstepX")
        acct = os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown")
        print(f"[BOT] Account: {acct}")

        for sym, st in self.states.items():
            st.ctx = self.suite[sym]
            st._suite_client = self.suite.client
            price = await st.ctx.data.get_current_price()
            st.last_price = price
            print(f"[BOT] {sym} contract: {st.ctx.instrument_info.id}")
            print(f"[BOT] {sym} price: {price:.2f}")

            if st.stddev.daily_stdev is None:
                st.stddev.compute_stdev_from_history()
            if st.stddev.session_open is None:
                st.stddev.set_session_open(price)
            # Initial PnL sync
            await st._sync_pnl_from_platform()

        await self._auto_detect_practice_account(symbols)

        print(f"[BOT] Session active: {in_session()}")
        print(f"[BOT] Trading LIVE — StdDev EMA Flip strategy ({', '.join(symbols)})")
        print(f"[BOT] Watchdog: {WATCHDOG_TIMEOUT}s timeout")
        print(f"[BOT] Press Ctrl+C to stop")

        # Send startup message
        for sym, st in self.states.items():
            stats = st.ml.stats()
            level_0 = st.stddev.get_level("0") or 0
            level_05 = st.stddev.get_level("0.5") or 0
            level_m05 = st.stddev.get_level("-0.5") or 0
            msg = (f"STATUS|StdDev VWAP Bot started\n"
                   f"Account: {acct}\n"
                   f"0-level: {level_0:.2f}\n"
                   f"0.5σ (Long TP): {level_05:.2f}\n"
                   f"-0.5σ (Short TP): {level_m05:.2f}\n"
                   f"VWAP TF: {CANDLE_MINUTES}min\n"
                   f"{stats}")
            threading.Thread(target=send_telegram, args=(
                self.tg_token, self.tg_chat, msg), daemon=True).start()

        # Main tick loop
        self.last_tick_time = time.time()
        save_interval = 30
        last_save = time.time()
        last_status = time.time()
        status_interval = 300  # 5 min status prints
        gc_interval = 120
        last_gc = time.time()

        while self.running:
            if not in_session():
                await asyncio.sleep(5)
                self.last_tick_time = time.time()
                continue

            if in_blackout():
                await asyncio.sleep(1)
                self.last_tick_time = time.time()
                continue

            # Process each symbol
            for sym, st in self.states.items():
                try:
                    price = await asyncio.wait_for(
                        st.ctx.data.get_current_price(), timeout=5.0)
                except Exception:
                    continue

                if price is None or price <= 0:
                    continue

                self.last_tick_time = time.time()
                actions = st.tick(price, self.last_tick_time)

                for action in actions:
                    if action[0] == "enter_long":
                        await st._enter_long(action[1])
                    elif action[0] == "enter_short":
                        await st._enter_short(action[1])
                    elif action[0] == "flatten":
                        reason = action[2] if len(action) > 2 else "signal"
                        await st._flatten(action[1], reason)

            # Periodic saves
            now = time.time()
            if now - last_save > save_interval:
                self.save_all_state()
                last_save = now

            # Status prints
            if now - last_status > status_interval:
                for sym, st in self.states.items():
                    vwap_str = f"{st.candles.vwap:.2f}" if st.candles.vwap else "N/A"
                    l0 = st.stddev.get_level("0")
                    l05 = st.stddev.get_level("0.5")
                    lm05 = st.stddev.get_level("-0.5")
                    pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[st.position]
                    zone = st.stddev.get_zone(st.last_price)
                    pvw = st.get_price_vs_vwap()
                    print(f"  [{sym} @ {datetime.now(ET).strftime('%H:%M:%S')}]")
                    print(f"    Price: {st.last_price:.2f} | VWAP: {vwap_str} | PvsVWAP: {pvw:+.1f} | Zone: {zone}")
                    print(f"    0: {l0:.2f} | 0.5: {l05:.2f} | -0.5: {lm05:.2f}" if l0 else "    Levels: N/A")
                    print(f"    Position: {pos_str} x{st.contracts_held} | "
                          f"P&L: ${st.live_pnl:.2f} | Flip #{st.flip_count} | "
                          f"PV=${st.pv}/pt")
                    print(f"    {st.ml.stats()}")
                last_status = now

            # GC
            if now - last_gc > gc_interval:
                gc.collect()
                import resource
                rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] [MEM] RSS: {rss_mb:.0f}MB (gc collected)")
                last_gc = now

            # Watchdog
            if time.time() - self.last_tick_time > WATCHDOG_TIMEOUT:
                print(f"[WATCHDOG] Main loop stuck for {time.time() - self.last_tick_time:.0f}s — killing process")
                self.save_all_state()
                import sys
                sys.exit(1)

            await asyncio.sleep(1)  # 1-second tick interval


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
    parser = argparse.ArgumentParser(description="TopstepX StdDev EMA Flip Bot")
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

    print(f"[BOT] StdDev VWAP RL Bot")
    print(f"[BOT] ENTRY: RL decides from {CANDLE_MINUTES}min VWAP + StdDev zone context")
    print(f"[BOT] EXIT: Price touches ±0.5σ (TP), RL close, or flip")
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

    retry_delay = 30
    while not stopped:
        bot = StdDevBot(
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
                          f"STATUS|StdDev VWAP bot crashed, restarting in {retry_delay}s")
            retry_delay = min(retry_delay * 2, 300)
        finally:
            bot.save_all_state()
            loop.close()

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
