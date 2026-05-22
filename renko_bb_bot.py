"""
TopstepX BB Mean-Reversion Bot (LIVE)
======================================
Strategy: Bollinger Band(20, 1.5) mean-reversion on 30-second candles
- Compute BB(20, SMA, close, 1.5 StdDev, ddof=0) on 30s candle closes — matches TradingView
- Entry (mean-reversion):
    RED candle (close < open) AND close is ABOVE upper BB  → enter SHORT
    GREEN candle (close > open) AND close is BELOW lower BB → enter LONG
- Exit (candle failure):
    LONG position: new RED candle closes BELOW lower BB  → exit
    SHORT position: new GREEN candle closes ABOVE upper BB → exit
- RL learns to adjust SL/TP tiers over time via Q-learning
- Fixed-point SL/TP checked on every tick; candle-failure stop on candle close
- Re-entry possible immediately on the next qualifying candle

Usage:
    python renko_bb_bot.py --symbols "NQ:1" --tick-interval 1
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

SESSION_START = dtime(18, 0, 0)    # 6:00 PM ET start
SESSION_END = dtime(16, 0, 0)      # 4:00 PM ET end
TRADING_DAYS = [0, 1, 2, 3, 4, 6]  # Mon-Fri + Sun
BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

TRADE_SESSION_START = dtime(18, 0, 0)
TRADE_SESSION_END = dtime(16, 0, 0)

# BB settings — matches TradingView: BB(20, SMA, close, 1.5, ddof=0)
BB_LENGTH = 20
BB_MULT = 1.5
CANDLE_SECONDS = 30  # 30-second candles

# SL/TP tiers the RL can learn to select
# 0 = default behavior (candle failure for SL, no fixed TP)
SL_TIERS = [0, 3, 5, 8, 12, 20]      # points
TP_TIERS = [0, 5, 10, 20, 40, 80]    # points

# Risk management
MAX_LOSS_DOLLARS = 300.0
QUICK_EXIT_DOLLARS = 150.0
QUICK_EXIT_SECS = 30
DAILY_LOSS_LIMIT = 1000.0

MINTICK_VALUES = {
    "NQ": 0.25, "ES": 0.25, "MNQ": 0.25, "MES": 0.25,
    "YM": 1.0, "RTY": 0.10,
}
POINT_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0,
    "YM": 5.0, "RTY": 10.0,
}

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
    if TRADE_SESSION_START > TRADE_SESSION_END:
        return t >= TRADE_SESSION_START or t < TRADE_SESSION_END
    return TRADE_SESSION_START <= t < TRADE_SESSION_END


# ============================================================
# 30-Second Candle Builder
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
            completed["direction"] = "green" if completed["close"] >= completed["open"] else "red"
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

    def get_closes(self, n=None):
        closes = [c["close"] for c in self.candles]
        return closes[-n:] if n else closes

    def get_momentum(self, lookback=5):
        closes = [c["close"] for c in self.candles]
        if len(closes) < lookback + 1:
            return 0.0
        return (closes[-1] - closes[-(lookback + 1)]) / lookback


# ============================================================
# Bollinger Bands Calculator
# ============================================================

class BollingerBands:
    """BB(20, 1.5) with population stddev (ddof=0) matching TradingView."""

    def __init__(self, length: int = BB_LENGTH, mult: float = BB_MULT):
        self.length = length
        self.mult = mult

    def compute(self, closes: list) -> dict:
        if len(closes) < self.length:
            return None
        window = closes[-self.length:]
        basis = sum(window) / len(window)
        variance = sum((x - basis) ** 2 for x in window) / len(window)  # ddof=0
        std_dev = variance ** 0.5
        upper = basis + self.mult * std_dev
        lower = basis - self.mult * std_dev
        return {"basis": basis, "upper": upper, "lower": lower,
                "std_dev": std_dev, "width": upper - lower}


# ============================================================
# Reinforcement Learning (Q-Learning) with SL/TP Learning
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
EARLY_EXIT_COOLDOWN = 10
SLTP_WARMUP = 40  # trades before RL adjusts SL/TP


class TradeML:
    """Q-learning RL for 30s candle BB reversal with SL/TP tier learning."""

    ACTION_ENTER = 0
    ACTION_SKIP = 1
    ACTION_WAIT = 2
    ACTION_FLIP = 3

    def __init__(self, data_file: str):
        self.data_file = data_file
        self.q_table = {}
        self.sl_q = {}      # state -> [Q for each SL tier]
        self.tp_q = {}      # state -> [Q for each TP tier]
        self.trades = []
        self.epsilon = RL_EPSILON_START
        self.total_trades = 0
        self.recent_outcomes = []
        self.regime_shift = False
        self.state_mae = {}
        self.state_mfe = {}
        self._load()

    def _state_key(self, features: dict) -> str:
        """Discretize: bb_pos|bb_width|momentum|time_zone|breakout_str|streak"""
        bb_pos = features.get("bb_position", "middle")

        bb_w = features.get("bb_width_pct", 0.5)
        if bb_w > 1.0:
            width_zone = "wide"
        elif bb_w > 0.4:
            width_zone = "medium"
        else:
            width_zone = "tight"

        mom = features.get("momentum", "flat")

        hour = features.get("hour", 12)
        if hour < 10:
            time_zone = "early"
        elif hour < 12:
            time_zone = "morning"
        elif hour < 14:
            time_zone = "midday"
        else:
            time_zone = "afternoon"

        breakout = features.get("breakout_strength", "none")

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

        return f"{bb_pos}|{width_zone}|{mom}|{time_zone}|{breakout}|{streak}"

    def _get_q(self, state_key: str) -> list:
        if state_key not in self.q_table:
            self.q_table[state_key] = [0.0, 0.0, 0.0, 0.0]
        q = self.q_table[state_key]
        while len(q) < 4:
            q.append(0.0)
        return q

    def _get_sl_q(self, state_key: str) -> list:
        if state_key not in self.sl_q:
            self.sl_q[state_key] = [0.0] * len(SL_TIERS)
        q = self.sl_q[state_key]
        while len(q) < len(SL_TIERS):
            q.append(0.0)
        return q

    def _get_tp_q(self, state_key: str) -> list:
        if state_key not in self.tp_q:
            self.tp_q[state_key] = [0.0] * len(TP_TIERS)
        q = self.tp_q[state_key]
        while len(q) < len(TP_TIERS):
            q.append(0.0)
        return q

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r") as f:
                    data = json.load(f)
                self.q_table = data.get("q_table", {})
                self.sl_q = data.get("sl_q", {})
                self.tp_q = data.get("tp_q", {})
                self.trades = data.get("trades", [])
                self.epsilon = data.get("epsilon", RL_EPSILON_START)
                self.total_trades = data.get("total_trades", len(self.trades))
                self.recent_outcomes = data.get("recent_outcomes", [])
                self.regime_shift = data.get("regime_shift", False)
                self.state_mae = data.get("state_mae", {})
                self.state_mfe = data.get("state_mfe", {})
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
                "sl_q": self.sl_q,
                "tp_q": self.tp_q,
                "trades": self.trades[-500:],
                "epsilon": self.epsilon,
                "total_trades": self.total_trades,
                "recent_outcomes": self.recent_outcomes[-20:],
                "regime_shift": self.regime_shift,
                "state_mae": trimmed_mae,
                "state_mfe": {k: v[-50:] for k, v in self.state_mfe.items()},
            }
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.data_file)
        except Exception as e:
            print(f"[RL] Save error: {e}")

    def extract_features(self, price: float, bb: dict, candle_closes: list,
                         armed_direction: str = None, armed_strength: float = 0.0) -> dict:
        hour = datetime.now(ET).hour

        if bb is None:
            bb_pos = "no_bb"
            bb_width_pct = 0.5
        else:
            bb_range = bb["upper"] - bb["lower"]
            if bb["basis"] > 0:
                bb_width_pct = (bb_range / bb["basis"]) * 100.0
            else:
                bb_width_pct = 0.5

            dist_from_lower = price - bb["lower"]
            dist_from_upper = bb["upper"] - price
            if price < bb["lower"]:
                bb_pos = "below_lower"
            elif dist_from_lower < bb_range * 0.15:
                bb_pos = "near_lower"
            elif price > bb["upper"]:
                bb_pos = "above_upper"
            elif dist_from_upper < bb_range * 0.15:
                bb_pos = "near_upper"
            else:
                bb_pos = "middle"

        # Momentum from last 5 candle closes
        if len(candle_closes) >= 6:
            mom_pts = (candle_closes[-1] - candle_closes[-6]) / 5.0
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

        # Breakout strength (distance outside BB)
        if armed_strength > 10:
            breakout_str = "strong"
        elif armed_strength > 3:
            breakout_str = "moderate"
        elif armed_strength > 0:
            breakout_str = "weak"
        else:
            breakout_str = "none"

        vol = 0.0
        if len(candle_closes) >= 10:
            vol = float(np.std(candle_closes[-10:]))

        return {
            "bb_position": bb_pos,
            "bb_width_pct": round(bb_width_pct, 3),
            "momentum": momentum,
            "hour": hour,
            "breakout_strength": breakout_str,
            "price": round(price, 2),
            "bb_basis": round(bb["basis"], 2) if bb else None,
            "bb_upper": round(bb["upper"], 2) if bb else None,
            "bb_lower": round(bb["lower"], 2) if bb else None,
            "momentum_pts": round(mom_pts, 2),
            "volatility": round(vol, 4),
            "armed_direction": armed_direction,
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

    def select_sl_tp(self, features: dict) -> tuple:
        """Select SL/TP tiers. Returns (sl_pts, tp_pts, sl_idx, tp_idx)."""
        if self.total_trades < SLTP_WARMUP:
            return 0, 0, 0, 0  # defaults during warmup

        state = self._state_key(features)
        sl_q = self._get_sl_q(state)
        tp_q = self._get_tp_q(state)

        if random.random() < self.epsilon:
            sl_idx = random.randint(0, len(SL_TIERS) - 1)
            tp_idx = random.randint(0, len(TP_TIERS) - 1)
        else:
            sl_idx = sl_q.index(max(sl_q))
            tp_idx = tp_q.index(max(tp_q))

        return SL_TIERS[sl_idx], TP_TIERS[tp_idx], sl_idx, tp_idx

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
                     entry_action: str = "enter", mae: float = 0.0, mfe: float = 0.0,
                     sl_idx: int = 0, tp_idx: int = 0):
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

        self.regime_shift = self._detect_regime_shift()
        alpha = RL_ALPHA_REGIME if self.regime_shift else RL_ALPHA
        self._decay_q_table()

        # Update main action Q-table
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

        # Update SL/TP Q-tables
        sl_q = self._get_sl_q(state)
        tp_q = self._get_tp_q(state)
        if sl_idx < len(sl_q):
            sl_q[sl_idx] = sl_q[sl_idx] + alpha * (reward - sl_q[sl_idx])
        if tp_idx < len(tp_q):
            tp_q[tp_idx] = tp_q[tp_idx] + alpha * (reward - tp_q[tp_idx])

        self.recent_outcomes.append(pnl)
        if len(self.recent_outcomes) > 20:
            self.recent_outcomes = self.recent_outcomes[-20:]

        trade = {
            "features": features, "state": state, "pnl": pnl,
            "win": 1 if pnl > 0 else 0, "entry_action": entry_action,
            "q_after": list(q_vals), "alpha_used": alpha,
            "regime_shift": self.regime_shift, "epsilon": self.epsilon,
            "source": source, "timestamp": datetime.now(ET).isoformat(),
            "sl_idx": sl_idx, "tp_idx": tp_idx,
            "sl_pts": SL_TIERS[sl_idx] if sl_idx < len(SL_TIERS) else 0,
            "tp_pts": TP_TIERS[tp_idx] if tp_idx < len(TP_TIERS) else 0,
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
        sl_str = f"SL={SL_TIERS[sl_idx]}pts" if sl_idx < len(SL_TIERS) and SL_TIERS[sl_idx] > 0 else "SL=candle_fail"
        tp_str = f"TP={TP_TIERS[tp_idx]}pts" if tp_idx < len(TP_TIERS) and TP_TIERS[tp_idx] > 0 else "TP=RL"
        print(f"[RL] Trade recorded: PnL=${pnl:.2f} | {entry_action} | {sl_str} | {tp_str} | "
              f"state={state} | eps={self.epsilon:.3f} | "
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

        # Market data — 30-second candle builder
        self.candles = CandleBuilder(CANDLE_SECONDS)
        self.bb = BollingerBands(BB_LENGTH, BB_MULT)
        self.last_price = 0.0
        self.last_tick_time = 0.0
        self.last_bb = None  # most recent BB values

        # Position state
        self.position = 0
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_features = None
        self.entry_action = "enter"
        self.entry_bb = None         # BB values at entry (for candle failure stop)
        self.active_sl_pts = 0       # RL-selected SL in points (0 = candle failure)
        self.active_tp_pts = 0       # RL-selected TP in points (0 = no fixed TP)
        self.active_sl_idx = 0
        self.active_tp_idx = 0
        self.live_pnl = 0.0
        self.trade_mae = 0.0
        self.trade_mfe = 0.0

        # RL
        self.ml = TradeML(os.path.join(os.getcwd(), f"rl_state_{symbol}_30s_bb.json"))

        # TopstepX context
        self.ctx = None
        self._suite_client = None
        self._pending_rl = None
        self._start_balance = None
        self._pnl_session_day = None

        # Daily loss tracking
        self.daily_loss = 0.0
        self._save_counter = 0

    def extract_features(self) -> dict:
        closes = self.candles.get_closes(BB_LENGTH + 10)
        return self.ml.extract_features(
            price=self.last_price, bb=self.last_bb,
            candle_closes=closes,
            armed_direction=None,
            armed_strength=0.0)

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
        sl_pts, tp_pts, sl_idx, tp_idx = self.ml.select_sl_tp(features)
        sl_str = f"SL={sl_pts}pts" if sl_pts > 0 else "SL=candle_fail"
        tp_str = f"TP={tp_pts}pts" if tp_pts > 0 else "TP=RL_managed"

        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG @ {price:.2f} | "
              f"{sl_str} | {tp_str} | Session P&L: ${self.live_pnl:.2f}")

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
                self.entry_features = features
                self.entry_action = "enter"
                self.entry_bb = dict(self.last_bb) if self.last_bb else None
                self.active_sl_pts = sl_pts
                self.active_tp_pts = tp_pts
                self.active_sl_idx = sl_idx
                self.active_tp_idx = tp_idx
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

    async def _enter_short(self, price: float):
        if self.position != 0:
            return False
        await self._ensure_flat_before_entry()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")

        features = self.extract_features()
        sl_pts, tp_pts, sl_idx, tp_idx = self.ml.select_sl_tp(features)
        sl_str = f"SL={sl_pts}pts" if sl_pts > 0 else "SL=candle_fail"
        tp_str = f"TP={tp_pts}pts" if tp_pts > 0 else "TP=RL_managed"

        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT @ {price:.2f} | "
              f"{sl_str} | {tp_str} | Session P&L: ${self.live_pnl:.2f}")

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
                self.entry_features = features
                self.entry_action = "enter"
                self.entry_bb = dict(self.last_bb) if self.last_bb else None
                self.active_sl_pts = sl_pts
                self.active_tp_pts = tp_pts
                self.active_sl_idx = sl_idx
                self.active_tp_idx = tp_idx
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

        # Record to RL
        feat = self._pending_rl["features"] if self._pending_rl else self.entry_features
        ea = self._pending_rl.get("entry_action", self.entry_action) if self._pending_rl else self.entry_action
        sl_i = self._pending_rl.get("sl_idx", self.active_sl_idx) if self._pending_rl else self.active_sl_idx
        tp_i = self._pending_rl.get("tp_idx", self.active_tp_idx) if self._pending_rl else self.active_tp_idx
        m = self._pending_rl.get("mae", self.trade_mae) if self._pending_rl else self.trade_mae
        mf = self._pending_rl.get("mfe", self.trade_mfe) if self._pending_rl else self.trade_mfe

        if feat:
            self.ml.record_trade(feat, trade_pnl, source="live",
                                 entry_action=ea, mae=m, mfe=mf,
                                 sl_idx=sl_i, tp_idx=tp_i)

        self._pending_rl = None
        self.entry_features = None
        self.entry_bb = None

    def tick(self, price: float, ts: float = None):
        """Process a tick. 30s candle BB mean-reversion strategy."""
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
            print(f"[{self.symbol} SESSION] New session day {session_day} — all reset")

        # Feed tick to candle builder — returns completed candle or None
        completed_candle = self.candles.feed(price, ts)

        # Track MAE/MFE for open position (tick-level)
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
                print(f"[{now_str}] [{self.symbol} QUICK-EXIT] ${unrealized:.0f} in {elapsed:.0f}s")
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "sl_idx": self.active_sl_idx,
                                    "tp_idx": self.active_tp_idx,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                actions.append(("flatten", price, "QUICK_EXIT"))
                return actions

            # Max loss
            if unrealized < -MAX_LOSS_DOLLARS:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} MAX-LOSS] ${unrealized:.0f}")
                self._pending_rl = {"features": self.entry_features,
                                    "entry_action": self.entry_action,
                                    "sl_idx": self.active_sl_idx,
                                    "tp_idx": self.active_tp_idx,
                                    "mae": self.trade_mae, "mfe": self.trade_mfe}
                actions.append(("flatten", price, "MAX_LOSS"))
                return actions

            # Fixed SL check (RL-selected, on every tick)
            if self.active_sl_pts > 0:
                if self.position == 1:
                    sl_price = self.entry_price - self.active_sl_pts
                    if price <= sl_price:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} SL-HIT] LONG SL={self.active_sl_pts}pts @ {price:.2f}")
                        self._pending_rl = {"features": self.entry_features,
                                            "entry_action": self.entry_action,
                                            "sl_idx": self.active_sl_idx,
                                            "tp_idx": self.active_tp_idx,
                                            "mae": self.trade_mae, "mfe": self.trade_mfe}
                        actions.append(("flatten", price, f"SL_{self.active_sl_pts}pts"))
                        return actions
                else:
                    sl_price = self.entry_price + self.active_sl_pts
                    if price >= sl_price:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} SL-HIT] SHORT SL={self.active_sl_pts}pts @ {price:.2f}")
                        self._pending_rl = {"features": self.entry_features,
                                            "entry_action": self.entry_action,
                                            "sl_idx": self.active_sl_idx,
                                            "tp_idx": self.active_tp_idx,
                                            "mae": self.trade_mae, "mfe": self.trade_mfe}
                        actions.append(("flatten", price, f"SL_{self.active_sl_pts}pts"))
                        return actions

            # Fixed TP check (RL-selected, on every tick)
            if self.active_tp_pts > 0:
                if self.position == 1:
                    tp_price = self.entry_price + self.active_tp_pts
                    if price >= tp_price:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} TP-HIT] LONG TP={self.active_tp_pts}pts @ {price:.2f}")
                        self._pending_rl = {"features": self.entry_features,
                                            "entry_action": self.entry_action,
                                            "sl_idx": self.active_sl_idx,
                                            "tp_idx": self.active_tp_idx,
                                            "mae": self.trade_mae, "mfe": self.trade_mfe}
                        actions.append(("flatten", price, f"TP_{self.active_tp_pts}pts"))
                        return actions
                else:
                    tp_price = self.entry_price - self.active_tp_pts
                    if price <= tp_price:
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"[{now_str}] [{self.symbol} TP-HIT] SHORT TP={self.active_tp_pts}pts @ {price:.2f}")
                        self._pending_rl = {"features": self.entry_features,
                                            "entry_action": self.entry_action,
                                            "sl_idx": self.active_sl_idx,
                                            "tp_idx": self.active_tp_idx,
                                            "mae": self.trade_mae, "mfe": self.trade_mfe}
                        actions.append(("flatten", price, f"TP_{self.active_tp_pts}pts"))
                        return actions

            # RL early exit (checked on ticks, not bricks)
            elapsed = ts - self.entry_time
            if elapsed > EARLY_EXIT_COOLDOWN:
                should_exit, reason, _ = self.ml.should_early_exit(
                    self.entry_features, unrealized)
                if should_exit:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} EARLY-EXIT] {reason}")
                    self._pending_rl = {"features": self.entry_features,
                                        "entry_action": self.entry_action,
                                        "sl_idx": self.active_sl_idx,
                                        "tp_idx": self.active_tp_idx,
                                        "mae": self.trade_mae, "mfe": self.trade_mfe}
                    actions.append(("flatten", price, "EARLY_EXIT"))
                    return actions

        # --- Process completed candle ---
        if completed_candle is not None:
            closes = self.candles.get_closes()
            bb = self.bb.compute(closes)
            if bb is None:
                return actions
            self.last_bb = bb

            candle_close = completed_candle["close"]
            candle_open = completed_candle["open"]
            candle_direction = completed_candle["direction"]  # "green" or "red"
            now_str = datetime.now(ET).strftime("%H:%M:%S")

            # --- If in position: check candle failure stop (default SL=0) ---
            if self.position != 0 and self.active_sl_pts == 0:
                if self.position == 1 and candle_direction == "red" and candle_close < bb["lower"]:
                    print(f"[{now_str}] [{self.symbol} CANDLE-FAIL] LONG: red candle closed "
                          f"{candle_close:.2f} < lower BB {bb['lower']:.2f}")
                    self._pending_rl = {"features": self.entry_features,
                                        "entry_action": self.entry_action,
                                        "sl_idx": self.active_sl_idx,
                                        "tp_idx": self.active_tp_idx,
                                        "mae": self.trade_mae, "mfe": self.trade_mfe}
                    actions.append(("flatten", price, "CANDLE_FAIL"))
                    return actions
                elif self.position == -1 and candle_direction == "green" and candle_close > bb["upper"]:
                    print(f"[{now_str}] [{self.symbol} CANDLE-FAIL] SHORT: green candle closed "
                          f"{candle_close:.2f} > upper BB {bb['upper']:.2f}")
                    self._pending_rl = {"features": self.entry_features,
                                        "entry_action": self.entry_action,
                                        "sl_idx": self.active_sl_idx,
                                        "tp_idx": self.active_tp_idx,
                                        "mae": self.trade_mae, "mfe": self.trade_mfe}
                    actions.append(("flatten", price, "CANDLE_FAIL"))
                    return actions

            # --- If in position: RL hold/flip check on candle close ---
            if self.position != 0:
                features = self.extract_features()
                action_str, _, reason = self.ml.should_skip(features)
                direction = "LONG" if self.position == 1 else "SHORT"

                if action_str == "flip":
                    print(f"[{now_str}] [{self.symbol} RL-FLIP] {direction} | {reason}")
                    self._pending_rl = {"features": self.entry_features,
                                        "entry_action": self.entry_action,
                                        "sl_idx": self.active_sl_idx,
                                        "tp_idx": self.active_tp_idx,
                                        "mae": self.trade_mae, "mfe": self.trade_mfe}
                    if self.position == 1:
                        actions.append(("flatten", price, "RL_FLIP_SHORT"))
                        actions.append(("enter_short", price))
                    else:
                        actions.append(("flatten", price, "RL_FLIP_LONG"))
                        actions.append(("enter_long", price))
                    return actions

                elif action_str == "skip":
                    position_aligned = (self.position == 1 and candle_close > bb["basis"]) or \
                                       (self.position == -1 and candle_close < bb["basis"])
                    if not position_aligned:
                        print(f"[{now_str}] [{self.symbol} RL-CLOSE] {direction} misaligned | {reason}")
                        self._pending_rl = {"features": self.entry_features,
                                            "entry_action": self.entry_action,
                                            "sl_idx": self.active_sl_idx,
                                            "tp_idx": self.active_tp_idx,
                                            "mae": self.trade_mae, "mfe": self.trade_mfe}
                        actions.append(("flatten", price, "RL_CLOSE"))
                        return actions

            if not in_trade_session():
                return actions

            # --- Entry logic (mean-reversion) when flat ---
            if self.position == 0:
                # RED candle closes ABOVE upper BB → SHORT
                if candle_direction == "red" and candle_close > bb["upper"]:
                    strength = candle_close - bb["upper"]
                    print(f"[{now_str}] [{self.symbol} SIGNAL-SHORT] Red 30s candle above upper BB: "
                          f"close={candle_close:.2f} > upper={bb['upper']:.2f} "
                          f"(strength: {strength:.2f}pts)")
                    features = self.extract_features()
                    action_str, _, reason = self.ml.should_skip(features)
                    print(f"  RL decision: {reason}")
                    if action_str in ("enter", "flip"):
                        actions.append(("enter_short", price))
                        return actions
                    else:
                        print(f"  SHORT signal SKIPPED by RL")

                # GREEN candle closes BELOW lower BB → LONG
                elif candle_direction == "green" and candle_close < bb["lower"]:
                    strength = bb["lower"] - candle_close
                    print(f"[{now_str}] [{self.symbol} SIGNAL-LONG] Green 30s candle below lower BB: "
                          f"close={candle_close:.2f} < lower={bb['lower']:.2f} "
                          f"(strength: {strength:.2f}pts)")
                    features = self.extract_features()
                    action_str, _, reason = self.ml.should_skip(features)
                    print(f"  RL decision: {reason}")
                    if action_str in ("enter", "flip"):
                        actions.append(("enter_long", price))
                        return actions
                    else:
                        print(f"  LONG signal SKIPPED by RL")

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
            "entry_bb": self.entry_bb,
            "active_sl_pts": self.active_sl_pts,
            "active_tp_pts": self.active_tp_pts,
            "active_sl_idx": self.active_sl_idx,
            "active_tp_idx": self.active_tp_idx,
            "live_pnl": self.live_pnl,
            "trade_mae": self.trade_mae,
            "trade_mfe": self.trade_mfe,
            "daily_loss": self.daily_loss,
            "candle_data": self.candles.candles[-50:],
            "last_bb": self.last_bb,
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        age = time.time() - state.get("saved_at", 0)
        candle_data = state.get("candle_data", [])
        if candle_data:
            self.candles.candles = candle_data
        self.last_bb = state.get("last_bb")
        self.daily_loss = state.get("daily_loss", 0.0)

        if age < position_ttl:
            self.position = state.get("position", 0)
            self.contracts_held = state.get("contracts_held", 0)
            self.entry_price = state.get("entry_price", 0.0)
            self.entry_time = state.get("entry_time", 0.0)
            self.entry_features = state.get("entry_features")
            self.entry_action = state.get("entry_action", "enter")
            self.entry_bb = state.get("entry_bb")
            self.active_sl_pts = state.get("active_sl_pts", 0)
            self.active_tp_pts = state.get("active_tp_pts", 0)
            self.active_sl_idx = state.get("active_sl_idx", 0)
            self.active_tp_idx = state.get("active_tp_idx", 0)
            self.live_pnl = state.get("live_pnl", 0.0)
            self.trade_mae = state.get("trade_mae", 0.0)
            self.trade_mfe = state.get("trade_mfe", 0.0)
            bb_str = f"BB basis={self.last_bb['basis']:.2f}" if self.last_bb else "no BB yet"
            print(f"  [{self.symbol}] Restored: pos={self.position}, {bb_str}, "
                  f"candles={len(self.candles.candles)}")
            return True
        else:
            print(f"  [{self.symbol}] State too old ({age:.0f}s), cleared")
            return False


# ============================================================
# Main Bot
# ============================================================

class RenkoBBBot:
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
        self.state_file = os.path.join(os.getcwd(), "bot_state_30s_bb.json")
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
                        sl_i = st.active_sl_idx
                        tp_i = st.active_tp_idx

                        st.live_pnl += pnl_est
                        st.position = 0
                        st.contracts_held = 0
                        print(f"[WS] Platform closed {direction} {sym} — est PnL: ${pnl_est:.2f}")

                        threading.Thread(target=send_signals, args=(
                            self.tg_token, self.tg_chat, self.tg_keys,
                            "FLAT", sym, price, 0),
                            kwargs={"ntfy_topic": st.ntfy_topic,
                                    "tp_webhooks": st.tp_webhooks}, daemon=True).start()

                        async def _deferred_rl(st_ref, feat, pnl_before, ea, m, mf, si, ti):
                            try:
                                await st_ref._sync_pnl_from_platform()
                                real_pnl = st_ref.live_pnl - pnl_before + pnl_est
                                st_ref.ml.record_trade(feat, real_pnl, source="platform_close",
                                                       entry_action=ea, mae=m, mfe=mf,
                                                       sl_idx=si, tp_idx=ti)
                            except Exception:
                                st_ref.ml.record_trade(feat, pnl_est, source="platform_close_est",
                                                       entry_action=ea, mae=m, mfe=mf,
                                                       sl_idx=si, tp_idx=ti)

                        pnl_before = st.live_pnl - pnl_est
                        try:
                            loop = asyncio.get_event_loop()
                            loop.create_task(_deferred_rl(st, features, pnl_before,
                                                          entry_action, mae, mfe, sl_i, tp_i))
                        except Exception:
                            st.ml.record_trade(features, pnl_est, source="platform_close_est",
                                               entry_action=entry_action, mae=mae, mfe=mfe,
                                               sl_idx=sl_i, tp_idx=tp_i)
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
        print(f"[BOT] 30s Candle BB Mean-Reversion Bot starting...")
        print(f"[BOT] Strategy: BB({BB_LENGTH}, {BB_MULT}) mean-reversion on "
              f"{CANDLE_SECONDS}s candles")
        print(f"[BOT] ENTRY: RED candle above upper BB -> SHORT | GREEN candle below lower BB -> LONG")
        print(f"[BOT] EXIT: candle failure / RL SL/TP / RL close")
        print(f"[BOT] SL/TP: default=candle_failure, RL learns tiers after {SLTP_WARMUP} trades")
        print(f"[BOT] SL tiers: {SL_TIERS} pts | TP tiers: {TP_TIERS} pts")
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
        print(f"[BOT] Trading LIVE — 30s Candle BB Mean-Reversion strategy ({', '.join(symbols)})")
        print(f"[BOT] Watchdog: {WATCHDOG_TIMEOUT}s timeout")
        print(f"[BOT] Press Ctrl+C to stop")

        # Startup Telegram
        for sym, st in self.states.items():
            stats = st.ml.stats()
            msg = (f"STATUS|30s Candle BB Mean-Reversion Bot started\n"
                   f"Account: {acct}\n"
                   f"BB({BB_LENGTH}, {BB_MULT}) on {CANDLE_SECONDS}s candles\n"
                   f"SL: candle failure (RL learns tiers)\n"
                   f"{stats}")
            threading.Thread(target=send_telegram, args=(
                self.tg_token, self.tg_chat, msg), daemon=True).start()

        # Main tick loop
        self.last_tick_time = time.time()
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
                continue

            if in_blackout():
                await asyncio.sleep(1)
                self.last_tick_time = time.time()
                continue

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

            now = time.time()
            if now - last_save > save_interval:
                self.save_all_state()
                last_save = now

            if now - last_status > status_interval:
                for sym, st in self.states.items():
                    bb_str = (f"BB: {st.last_bb['lower']:.2f} / {st.last_bb['basis']:.2f} / "
                              f"{st.last_bb['upper']:.2f}") if st.last_bb else "BB: computing..."
                    pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[st.position]
                    print(f"  [{sym} @ {datetime.now(ET).strftime('%H:%M:%S')}]")
                    print(f"    Price: {st.last_price:.2f} | {bb_str}")
                    print(f"    Position: {pos_str} x{st.contracts_held} | "
                          f"P&L: ${st.live_pnl:.2f}")
                    print(f"    Candles: {len(st.candles.candles)} | {st.ml.stats()}")
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
    parser = argparse.ArgumentParser(description="TopstepX 30s Candle BB Mean-Reversion Bot")
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

    print(f"[BOT] 30s Candle BB Mean-Reversion Bot")
    print(f"[BOT] BB({BB_LENGTH}, {BB_MULT}) on {CANDLE_SECONDS}s candles")
    print(f"[BOT] ENTRY: red candle close > upper BB -> SHORT | green candle close < lower BB -> LONG")
    print(f"[BOT] EXIT: candle failure / RL SL/TP / RL close")
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
        """Runs outside the event loop — detects dead loop and force-kills process."""
        while not stopped:
            time.sleep(60)
            if current_bot and current_bot.last_tick_time > 0:
                gap = time.time() - current_bot.last_tick_time
                if gap > WATCHDOG_TIMEOUT + 60:
                    print(f"[THREAD-WATCHDOG] Event loop appears dead — no tick for {gap:.0f}s, force-killing")
                    current_bot.save_all_state()
                    os._exit(1)

    wd = threading.Thread(target=thread_watchdog, daemon=True)
    wd.start()

    retry_delay = 30
    while not stopped:
        bot = RenkoBBBot(
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
                          f"STATUS|30s Candle BB bot crashed, restarting in {retry_delay}s")
            retry_delay = min(retry_delay * 2, 300)
        finally:
            bot.save_all_state()
            loop.close()

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
