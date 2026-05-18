"""
TopstepX Renko MFI + Vortex Bot (LIVE)
========================================
Multi-symbol support: runs multiple instruments on one connection.

Strategy: MFI + Vortex Indicator gap on Renko bricks
- MFI(14) on Renko bricks (volume = 1 per brick)
- Vortex Indicator(14) on Renko bricks (VI+ and VI-)
- LONG:  MFI oversold + VI- >> VI+ (bounce expected) → enter immediately
- SHORT: MFI overbought + VI+ >> VI- (pullback expected) → enter immediately
- If Vortex gap too small → skip (weak signal)
- EARLY EXIT: RL tracks MAE per state, cuts trade if drawdown exceeds winner profile
- EXIT: opposite MFI signal flips the position, TP hit, or early exit.

Usage:
    python mfi_bb_bot.py --symbols "NQ:2:2:ntfy-topic" --tick-interval 1
"""

import asyncio
import argparse
import gc
import signal
import json
import os
import time
import threading
import csv
import urllib.request
import urllib.error
from datetime import datetime, time as dtime

import numpy as np
import pytz


# ============================================================
# Telegram / ntfy helpers
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
        payload = {
            "ticker": symbol,
            "action": action,
            "price": price,
            "quantity": qty,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data,
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
# Renko Engine (Traditional - matches TradingView traditional renko)
# ============================================================

class RenkoEngine:
    def __init__(self, brick_size: float, label: str = ""):
        self.brick_size = brick_size
        self.label = label
        self.last_close = None
        self.direction = 0
        self.brick_count = 0

    def initialize(self, price: float):
        if self.brick_size <= 0:
            self.last_close = price
            return
        self.last_close = round(price / self.brick_size) * self.brick_size

    def feed_close(self, close_price: float) -> list:
        """Returns list of (open, close, direction) tuples for any new bricks
        formed by this price update. Empty list if no new brick."""
        if self.last_close is None:
            self.initialize(close_price)
            return []
        if self.brick_size <= 0:
            return []

        new_bricks = []
        # Safety cap: prevent runaway loops on bad data / tiny brick_size
        MAX_BRICKS_PER_FEED = 10000
        iterations = 0
        while iterations < MAX_BRICKS_PER_FEED:
            iterations += 1
            if close_price >= self.last_close + self.brick_size:
                new_open = self.last_close
                new_close = self.last_close + self.brick_size
                new_bricks.append((new_open, new_close, 1))
                self.last_close = new_close
                self.direction = 1
                self.brick_count += 1
            elif close_price <= self.last_close - self.brick_size:
                new_open = self.last_close
                new_close = self.last_close - self.brick_size
                new_bricks.append((new_open, new_close, -1))
                self.last_close = new_close
                self.direction = -1
                self.brick_count += 1
            else:
                break
        if iterations >= MAX_BRICKS_PER_FEED:
            print(f"[RENKO {self.label}] WARNING: hit safety cap {MAX_BRICKS_PER_FEED} - "
                  f"input price={close_price}, brick_size={self.brick_size}")
        return new_bricks


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

# NQ futures schedule: Sun 18:00 ET -> Fri 17:00 ET (overnight session continuous).
# This is intentionally NOT 24/7 because futures have weekly maintenance windows.
SESSION_START = dtime(18, 0, 0)   # 6:00 PM ET start
SESSION_END = dtime(15, 30, 0)    # 3:30 PM ET end
TRADING_DAYS = [0, 1, 2, 3, 4, 6]  # Mon-Fri (0-4) + Sun (6). Saturday excluded.
# CME daily settlement halt: 4:15-4:30 PM ET (with buffer)
BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

# Indicator parameters
MFI_PERIOD = 14
MFI_OVERSOLD = 20.0
MFI_OVERBOUGHT = 80.0
VORTEX_PERIOD = 14
VORTEX_GAP_THRESHOLD = 0.50

# Smart early exit: cut losses when trade MAE exceeds what winners typically see
EARLY_EXIT_MULTIPLIER = 1.5   # exit if current MAE > avg winning MAE * this
EARLY_EXIT_MIN_SAMPLES = 3    # need this many winning trades per state to activate
EARLY_EXIT_COOLDOWN = 15      # seconds after entry before early exit can trigger
MAX_LOSS_DOLLARS = 200.0      # hard ceiling: flatten if unrealized loss exceeds this
QUICK_EXIT_DOLLARS = 100.0    # if this much red within QUICK_EXIT_SECS, exit immediately
QUICK_EXIT_SECS = 30          # window for quick exit check

# Dynamic TP: learn optimal take-profit per state from post-exit price movement
TP_DEFAULT_DOLLARS = 200.0    # starting TP target
TP_MIN_DOLLARS = 50.0         # never go below this
TP_MAX_DOLLARS = 800.0        # never go above this
TP_LEARN_MIN_SAMPLES = 5      # need this many trades per state to adjust TP
TP_POST_EXIT_TRACK_SECS = 60  # how long to track price after exit
TP_ADJUST_RATE = 0.2          # blend 20% of learned TP each trade

# Breakeven exit: take BE if trade recovers but state says unlikely to reach TP
BE_EXIT_THRESHOLD = 30.0      # within $30 of breakeven and recovering
BE_EXIT_MIN_SAMPLES = 5       # need this many trade samples per state
BE_EXIT_MIN_TIME = 90         # seconds in trade before BE exit can trigger
BE_RECOVERY_RATIO = 0.4       # if <40% of trades in this state that dip then recover hit TP

EMA_PERIOD = 9                # used as ML feature only (not for entry/exit)
RENKO_SMA_PERIOD = 12         # R.sg (Renko Smoothed Gradient) period

# Optional R.sg distance filter: skip MFI signals when last brick close is
# within `brick_size` of the R.sg line. Default off; enable via --use-rsg-filter.
RSG_DECAY_MULTIPLIER = 250

# Per-symbol point/tick values
MINTICK_VALUES = {
    "NQ": 0.25, "ES": 0.25, "MNQ": 0.25, "MES": 0.25,
    "YM": 1.0, "RTY": 0.10,
}
POINT_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0,
    "YM": 5.0, "RTY": 10.0,
}


def in_session() -> bool:
    """True if current ET time is within the trading session."""
    now = datetime.now(ET)
    if now.weekday() not in TRADING_DAYS:
        return False
    t = now.time()
    if SESSION_START > SESSION_END:
        in_main = t >= SESSION_START or t < SESSION_END
    else:
        in_main = SESSION_START <= t < SESSION_END
    return in_main


def in_blackout() -> bool:
    """True during CME daily halt — stay connected but don't trade."""
    if BLACKOUT_START == BLACKOUT_END:
        return False
    t = datetime.now(ET).time()
    return BLACKOUT_START <= t < BLACKOUT_END


# ============================================================
# Reinforcement Learning Trade Filter (Q-Learning)
# ============================================================

RL_WARMUP_TRADES = 20
RL_ALPHA = 0.15          # baseline learning rate
RL_ALPHA_REGIME = 0.30   # boosted alpha during regime shift
RL_GAMMA = 0.95          # discount factor
RL_EPSILON_START = 0.90  # initial exploration rate
RL_EPSILON_MIN = 0.10    # minimum exploration (always explore 10%)
RL_EPSILON_DECAY = 0.985 # decay per trade
RL_PNL_SCALE = 500.0     # normalize PnL rewards to ~[-1, 1] range
RL_Q_DECAY = 0.998       # per-trade decay toward zero (old knowledge fades)
RL_REGIME_WINDOW = 15    # recent trades to check for regime shift
RL_REGIME_THRESHOLD = 0.20  # if recent WR drops >20% vs overall, regime shift detected
PULLBACK_DEFAULT = 2.0   # default pullback pts before enough MAE data
PULLBACK_MAE_RATIO = 0.35  # use 35% of avg MAE as pullback target
PULLBACK_MIN = 1.0       # never wait for less than 1 pt
PULLBACK_MAX = 8.0       # never wait for more than 8 pts
PULLBACK_TIMEOUT = 30    # seconds to wait for pullback before cancelling

import random


class TradeML:
    """Q-learning RL filter. Discretizes market state, learns which
    states are profitable to enter, wait for pullback, skip, or flip."""

    ACTION_ENTER = 0
    ACTION_SKIP = 1
    ACTION_WAIT = 2
    ACTION_FLIP = 3

    def __init__(self, data_file: str):
        self.data_file = data_file
        self.q_table = {}       # state_key -> [Q(enter), Q(skip), Q(wait)]
        self.trades = []        # full trade history for stats
        self.epsilon = RL_EPSILON_START
        self.total_trades = 0
        self.recent_outcomes = []  # last 20 PnLs for streak/regime tracking
        self.regime_shift = False   # True when recent performance diverges from historical
        self.state_mae = {}     # state_key -> {"win_mae": [...], "lose_mae": [...]}
        self.state_mfe = {}    # state_key -> [max favorable excursion values]
        self.state_post_exit = {}  # state_key -> [post-exit price moves in trade direction]
        self.state_recovery = {}   # state_key -> {"dip_then_tp": N, "dip_then_fail": N}
        self._load()

    def _state_key(self, features: dict) -> str:
        """Discretize continuous features into a hashable state."""
        mfi = features.get("mfi_value", 50.0)
        if mfi <= 25:
            mfi_zone = "oversold"
        elif mfi >= 75:
            mfi_zone = "overbought"
        elif mfi <= 40:
            mfi_zone = "low"
        elif mfi >= 60:
            mfi_zone = "high"
        else:
            mfi_zone = "mid"

        mfi_vel = features.get("mfi_velocity", 0.0)
        vel_zone = "rising" if mfi_vel > 2.0 else "falling" if mfi_vel < -2.0 else "flat"

        vortex_gap = features.get("vortex_gap", 0.0)
        if vortex_gap >= 1.0:
            gap_zone = "huge"
        elif vortex_gap >= 0.7:
            gap_zone = "large"
        elif vortex_gap >= 0.5:
            gap_zone = "medium"
        else:
            gap_zone = "small"

        hour = features.get("hour", 12)
        if hour < 10:
            time_zone = "early"
        elif hour < 12:
            time_zone = "morning"
        elif hour < 14:
            time_zone = "midday"
        else:
            time_zone = "afternoon"

        vol = features.get("volatility", 0.0)
        vol_zone = "high" if vol > 3.0 else "low" if vol < 1.0 else "mid"

        direction = "long" if features.get("direction", 1) == 1 else "short"

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

        vwap_dist = features.get("vwap_distance", 0.0)
        if vwap_dist > 3.0:
            vwap_zone = "far_above"
        elif vwap_dist > 1.0:
            vwap_zone = "above"
        elif vwap_dist < -3.0:
            vwap_zone = "far_below"
        elif vwap_dist < -1.0:
            vwap_zone = "below"
        else:
            vwap_zone = "at_vwap"

        tr = features.get("tick_rate", 0.0)
        if tr >= 20.0:
            tick_zone = "fast"
        elif tr >= 8.0:
            tick_zone = "normal"
        else:
            tick_zone = "slow"

        return f"{direction}|{mfi_zone}|{vel_zone}|{gap_zone}|{time_zone}|{vol_zone}|{streak}|{vwap_zone}|{tick_zone}"

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
                states_explored = len(self.q_table)
                print(f"[RL] Loaded: {self.total_trades} trades, {states_explored} states, "
                      f"epsilon={self.epsilon:.3f}, {len(self.state_mae)} mae-states")
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
            trimmed_mfe = {k: v[-50:] for k, v in self.state_mfe.items()}
            trimmed_post = {k: v[-50:] for k, v in self.state_post_exit.items()}
            data = {
                "q_table": self.q_table,
                "trades": self.trades[-500:],
                "epsilon": self.epsilon,
                "total_trades": self.total_trades,
                "recent_outcomes": self.recent_outcomes[-20:],
                "regime_shift": self.regime_shift,
                "state_mae": trimmed_mae,
                "state_mfe": trimmed_mfe,
                "state_post_exit": trimmed_post,
                "state_recovery": self.state_recovery,
            }
            tmp = self.data_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.data_file)
        except Exception as e:
            print(f"[RL] Save error: {e}")

    def extract_features(self, direction: int, mfi_value, mfi_velocity,
                         price: float, ema, rsg, brick_size: float,
                         brick_closes: list, vortex_gap: float = 0.0,
                         vwap: float = None, tick_rate: float = 0.0) -> dict:
        mfi_v = float(mfi_value) if mfi_value is not None else 50.0
        mfi_vel = float(mfi_velocity) if mfi_velocity is not None else 0.0
        ema_distance = ((price - ema) / brick_size) if (ema is not None and brick_size > 0) else 0.0
        rsg_distance = ((price - rsg) / brick_size) if (rsg is not None and brick_size > 0) else 0.0
        hour = datetime.now(ET).hour

        vol = 0.0
        if len(brick_closes) >= 10:
            vol = float(np.std(brick_closes[-10:])) / brick_size if brick_size > 0 else 0.0

        vwap_dist = ((price - vwap) / brick_size) if (vwap is not None and brick_size > 0) else 0.0

        return {
            "direction": direction,
            "mfi_value": round(mfi_v, 2),
            "mfi_velocity": round(mfi_vel, 4),
            "rsg_distance": round(rsg_distance, 4),
            "ema_distance": round(ema_distance, 4),
            "hour": hour,
            "volatility": round(vol, 4),
            "vortex_gap": round(vortex_gap, 4),
            "vwap_distance": round(vwap_dist, 4),
            "tick_rate": round(float(tick_rate), 2),
        }

    def should_skip(self, features: dict) -> tuple:
        """Returns (action_str, value, reason).
        action_str: 'enter', 'skip', 'wait', or 'flip'."""
        if self.total_trades < RL_WARMUP_TRADES:
            return "enter", 0.0, f"RL warmup ({self.total_trades}/{RL_WARMUP_TRADES})"

        state = self._state_key(features)
        q_vals = self._get_q(state)
        q_enter, q_skip, q_wait, q_flip = q_vals[0], q_vals[1], q_vals[2], q_vals[3]

        # Epsilon-greedy: explore or exploit
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
        regime_tag = "|REGIME" if self.regime_shift else ""
        reason = (f"RL|{state}|Q(now)={q_enter:+.2f} Q(skip)={q_skip:+.2f} "
                  f"Q(wait)={q_wait:+.2f} Q(flip)={q_flip:+.2f}|eps={self.epsilon:.2f}|"
                  f"{choice}|{action_str}{regime_tag}")
        return action_str, 0.0, reason

    def _detect_regime_shift(self) -> bool:
        """Compare recent win rate vs overall. Large divergence = regime shift."""
        total = len(self.trades)
        if total < RL_REGIME_WINDOW + 10:
            return False
        overall_wr = sum(1 for t in self.trades if t["win"] == 1) / total
        recent = self.trades[-RL_REGIME_WINDOW:]
        recent_wr = sum(1 for t in recent if t["win"] == 1) / len(recent)
        drop = overall_wr - recent_wr
        return drop >= RL_REGIME_THRESHOLD

    def _decay_q_table(self):
        """Gradually decay all Q-values toward zero. Old knowledge fades."""
        for key in self.q_table:
            for i in range(len(self.q_table[key])):
                self.q_table[key][i] *= RL_Q_DECAY

    def record_trade(self, features: dict, pnl: float, source: str = "live",
                     entry_action: str = "enter", mae: float = 0.0, mfe: float = 0.0):
        if features is None:
            return

        state = self._state_key(features)
        reward = pnl / RL_PNL_SCALE

        # Store MAE per state for smart early exit
        if state not in self.state_mae:
            self.state_mae[state] = {"win_mae": [], "lose_mae": []}
        bucket = "win_mae" if pnl > 0 else "lose_mae"
        self.state_mae[state][bucket].append(mae)
        if len(self.state_mae[state][bucket]) > 50:
            self.state_mae[state][bucket] = self.state_mae[state][bucket][-50:]

        # Store MFE per state for dynamic TP learning
        if mfe > 0:
            if state not in self.state_mfe:
                self.state_mfe[state] = []
            self.state_mfe[state].append(mfe)
            if len(self.state_mfe[state]) > 50:
                self.state_mfe[state] = self.state_mfe[state][-50:]

        # Track recovery patterns: did trade dip then reach TP, or dip then fail?
        if mae < -30 and state not in self.state_recovery:
            self.state_recovery[state] = {"dip_then_tp": 0, "dip_then_fail": 0}
        if mae < -30:
            if pnl > 0:
                self.state_recovery[state]["dip_then_tp"] += 1
            else:
                self.state_recovery[state]["dip_then_fail"] += 1

        # Detect regime shift and pick alpha
        self.regime_shift = self._detect_regime_shift()
        alpha = RL_ALPHA_REGIME if self.regime_shift else RL_ALPHA

        # Decay all Q-values (old knowledge fades naturally)
        self._decay_q_table()

        # Update the Q-value for the action that was taken
        q_vals = self._get_q(state)
        action_map = {"enter": self.ACTION_ENTER, "wait": self.ACTION_WAIT,
                       "flip": self.ACTION_FLIP}
        action_idx = action_map.get(entry_action, self.ACTION_ENTER)
        old_q = q_vals[action_idx]
        q_vals[action_idx] = old_q + alpha * (reward - old_q)

        # If trade was a loser, boost Q(skip) slightly
        if pnl < 0:
            old_skip_q = q_vals[self.ACTION_SKIP]
            skip_reward = abs(reward) * 0.3
            q_vals[self.ACTION_SKIP] = old_skip_q + alpha * (skip_reward - old_skip_q)

        # If flip entry won, penalize normal enter (and vice versa)
        if entry_action == "flip" and pnl > 0:
            q_vals[self.ACTION_ENTER] += alpha * (-abs(reward) * 0.2)
        elif entry_action == "enter" and pnl > 0:
            q_vals[self.ACTION_FLIP] += alpha * (-abs(reward) * 0.1)

        # If wait entry got better PnL than avg, boost Q(wait) extra
        if entry_action == "wait" and pnl > 0:
            avg_pnl = sum(t["pnl"] for t in self.trades[-20:]) / max(len(self.trades[-20:]), 1) if self.trades else 0
            if pnl > avg_pnl:
                q_vals[self.ACTION_WAIT] += alpha * 0.1

        self.recent_outcomes.append(pnl)
        if len(self.recent_outcomes) > 20:
            self.recent_outcomes = self.recent_outcomes[-20:]

        regime_tag = " REGIME-SHIFT" if self.regime_shift else ""
        trade = {
            "features": features,
            "state": state,
            "pnl": pnl,
            "win": 1 if pnl > 0 else 0,
            "entry_action": entry_action,
            "q_after": list(q_vals),
            "alpha_used": alpha,
            "regime_shift": self.regime_shift,
            "epsilon": self.epsilon,
            "source": source,
            "timestamp": datetime.now(ET).isoformat(),
        }
        self.trades.append(trade)
        self.total_trades += 1

        # Decay epsilon (boost exploration during regime shift)
        if self.regime_shift:
            self.epsilon = min(0.50, self.epsilon + 0.05)
        else:
            self.epsilon = max(RL_EPSILON_MIN, self.epsilon * RL_EPSILON_DECAY)

        self._save()

        wins = sum(1 for t in self.trades if t["win"] == 1)
        total = len(self.trades)
        print(f"[RL] Trade recorded: PnL=${pnl:.2f} | {entry_action} | state={state} | "
              f"Q(now)={q_vals[0]:+.3f} Q(skip)={q_vals[1]:+.3f} Q(wait)={q_vals[2]:+.3f} Q(flip)={q_vals[3]:+.3f} | "
              f"alpha={alpha:.2f} | eps={self.epsilon:.3f} | {total} trades, "
              f"{wins} wins ({100 * wins / total:.0f}%){regime_tag}")

    def stats(self) -> str:
        if not self.trades:
            return "No trades recorded"
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"] == 1)
        losses = total - wins
        total_pnl = sum(t["pnl"] for t in self.trades)
        states = len(self.q_table)
        regime = " | REGIME-SHIFT" if self.regime_shift else ""
        return (f"RL: {total} trades | W:{wins} L:{losses} | "
                f"Win%: {100 * wins / total:.0f}% | PnL: ${total_pnl:.2f} | "
                f"{states} states | eps={self.epsilon:.3f}{regime}")

    def should_early_exit(self, features: dict, current_mae: float) -> tuple:
        """Check if current trade MAE exceeds what winners typically see in this state.
        Returns (should_exit: bool, reason: str, avg_win_mae: float)."""
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
                          f"(avg winner MAE ${avg_win_mae:.0f} x {EARLY_EXIT_MULTIPLIER}) | "
                          f"state={state} ({len(win_maes)} samples)"), avg_win_mae
        return False, "", avg_win_mae

    def get_dynamic_tp(self, features: dict, base_tp: float) -> tuple:
        """Calculate dynamic TP based on historical MFE for this state.
        Returns (tp_dollars, reason_str)."""
        if features is None:
            return base_tp, "default"
        state = self._state_key(features)
        mfe_list = self.state_mfe.get(state, [])
        if len(mfe_list) < TP_LEARN_MIN_SAMPLES:
            return base_tp, f"default (need {TP_LEARN_MIN_SAMPLES - len(mfe_list)} more samples)"
        avg_mfe = sum(mfe_list) / len(mfe_list)
        # Target 70% of average MFE (leave some on table but capture most)
        learned_tp = avg_mfe * 0.70
        # Blend with base: slowly move toward learned value
        blended = base_tp * (1 - TP_ADJUST_RATE) + learned_tp * TP_ADJUST_RATE
        blended = max(TP_MIN_DOLLARS, min(TP_MAX_DOLLARS, blended))
        return round(blended, 2), (f"dynamic TP: avg_mfe=${avg_mfe:.0f}, "
                                    f"learned=${learned_tp:.0f}, blended=${blended:.0f} "
                                    f"({len(mfe_list)} samples)")

    def record_post_exit(self, features: dict, post_exit_move: float):
        """Record how far price moved in trade direction after exit."""
        if features is None:
            return
        state = self._state_key(features)
        if state not in self.state_post_exit:
            self.state_post_exit[state] = []
        self.state_post_exit[state].append(post_exit_move)
        if len(self.state_post_exit[state]) > 50:
            self.state_post_exit[state] = self.state_post_exit[state][-50:]
        self._save()

    def should_breakeven_exit(self, features: dict, current_pnl: float,
                              had_drawdown: bool) -> tuple:
        """Check if we should take breakeven rather than wait for full TP.
        Returns (should_exit: bool, reason: str)."""
        if features is None or not had_drawdown:
            return False, ""
        if current_pnl < 0 or current_pnl > BE_EXIT_THRESHOLD:
            return False, ""
        state = self._state_key(features)
        recovery = self.state_recovery.get(state)
        if not recovery:
            return False, ""
        total_dips = recovery["dip_then_tp"] + recovery["dip_then_fail"]
        if total_dips < BE_EXIT_MIN_SAMPLES:
            return False, ""
        recovery_rate = recovery["dip_then_tp"] / total_dips
        if recovery_rate < BE_RECOVERY_RATIO:
            return True, (f"BE exit: pnl ${current_pnl:.0f}, recovery rate "
                          f"{recovery_rate:.0%} < {BE_RECOVERY_RATIO:.0%} "
                          f"({total_dips} samples) | state={state}")
        return False, ""


# ============================================================
# Per-Symbol Strategy State
# ============================================================

class SymbolState:
    def __init__(self, symbol: str, brick_size: float, qty: int,
                 ntfy_topic: str, tg_token: str, tg_chat: str, tg_keys: list,
                 tick_interval: int = 1, use_rsg_filter: bool = False,
                 tp_webhooks: list = None):
        # Config
        self.symbol = symbol
        self.qty = qty
        self.brick_size = brick_size
        self.ntfy_topic = ntfy_topic
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys
        self.tick_interval = tick_interval
        self.point_value = POINT_VALUES.get(symbol, 20.0)
        self.use_rsg_filter = use_rsg_filter
        self.tp_webhooks = tp_webhooks or []

        # Renko engine
        self.renko = RenkoEngine(brick_size, symbol)

        # Brick history
        self.brick_closes = []
        self.brick_opens = []
        self.brick_typicals = []
        # Volume = 1 per brick. Customer accepts that MFI values won't match TV
        # exactly because TopstepX doesn't expose real volume. Constant 1 is the
        # most predictable proxy and makes MFI a pure typical-price oscillator.
        self.brick_volumes = []

        # Indicators
        self.ema = None
        self.rsg_decay = RSG_DECAY_MULTIPLIER * MINTICK_VALUES.get(symbol, 0.25)
        self.rsg_dosc = None
        self.rsg_dosc_values = []
        self.renko_sma = None  # R.sg SMA over last RENKO_SMA_PERIOD values

        # Session VWAP (resets daily, uses tick count as volume proxy)
        self.vwap_cum_pv = 0.0    # cumulative (price * tick_count)
        self.vwap_cum_vol = 0      # cumulative tick count
        self.vwap = None
        self.vwap_session_date = None

        # Tick counting for volume proxy
        self.tick_count_this_brick = 0
        self.ticks_per_second = 0.0
        self.tick_window = []       # (timestamp, count) pairs for rate calc

        # MFI state
        self.mfi_value = None
        self.prev_mfi_value = None
        self.mfi_oversold_dot = False
        self.mfi_overbought_dot = False

        # Vortex Indicator(14) on Renko bricks
        self.vi_plus = None
        self.vi_minus = None

        # Position state (fixed qty, no scale-in)
        self.position = 0           # 1 long, -1 short, 0 flat
        self.contracts_held = 0
        self.entry_price = 0.0      # weighted average entry
        self.entry_time = None
        self.entry_features = None  # snapshot for ML
        self.live_pnl = 0.0
        self.trade_mae = 0.0        # max adverse excursion (worst drawdown $ during trade)
        self.trade_mfe = 0.0        # max favorable excursion (best unrealized $ during trade)
        self.mae_history = []        # list of MAE values for averaging
        self.TP_DOLLARS = TP_DEFAULT_DOLLARS
        self.current_tp = TP_DEFAULT_DOLLARS  # dynamic TP for current trade

        # Post-exit tracking (watches price after flatten to learn optimal TP)
        self.post_exit_tracking = False
        self.post_exit_direction = 0     # 1 long, -1 short
        self.post_exit_price = 0.0       # price at exit
        self.post_exit_time = 0.0        # when we exited
        self.post_exit_best = 0.0        # best price move in our direction after exit
        self.post_exit_features = None   # features at entry (for state lookup)

        # Pullback entry state (RL "wait" action)
        self.pending_direction = None   # "LONG" or "SHORT"
        self.pending_signal_price = 0.0 # price when signal fired
        self.pending_target_price = 0.0 # price we want to enter at
        self.pending_time = 0.0         # timestamp when pending started
        self.pending_features = None    # RL features snapshot
        self.entry_action = "enter"     # track which RL action was used
        self.early_exit_triggered = False  # prevent re-triggering
        self.had_significant_drawdown = False  # trade dipped significantly (for BE exit)

        # Connection / freshness tracking
        self.last_known_price = None
        self.last_price_change_time = None
        self.last_new_bar_time = None
        # Order error reporting: None | "rejected" | "timeout" | "error"
        self.last_order_error = None

        # Live price cache
        self.last_price = 0.0

        # Position-sync divergence counter (anti-flap for SDK get_all_positions bug)
        # Only act on platform_says_flat after N consecutive confirmations.
        self.platform_flat_streak = 0
        self.last_position_poll_time = 0
        self.POSITION_POLL_INTERVAL = 30   # seconds between HTTP polls
        self.PLATFORM_FLAT_THRESHOLD = 5    # need 5 consecutive flat reads to trust

        # File paths
        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"trade_log_{symbol}.jsonl"
        )

        # Wired by RenkoBot
        self.ml = None
        self.ctx = None

    # ----------------------------------------------------------------
    # State persistence
    # ----------------------------------------------------------------

    def save_state(self) -> dict:
        """Snapshot for crash recovery. Saved to disk every 30s."""
        return {
            "symbol": self.symbol,
            "schema": 2,  # bump if format changes
            "saved_at": time.time(),
            # indicator state (preserved across long downtimes)
            "brick_closes": self.brick_closes[-200:],
            "brick_opens": self.brick_opens[-200:],
            "brick_typicals": self.brick_typicals[-200:],
            "brick_volumes": self.brick_volumes[-200:],
            "ema": self.ema,
            "rsg_dosc": self.rsg_dosc,
            "rsg_dosc_values": self.rsg_dosc_values[-200:],
            "renko_sma": self.renko_sma,
            "mfi_value": self.mfi_value,
            "prev_mfi_value": self.prev_mfi_value,
            "mfi_oversold_dot": self.mfi_oversold_dot,
            "mfi_overbought_dot": self.mfi_overbought_dot,
            # Vortex state
            "vi_plus": self.vi_plus,
            "vi_minus": self.vi_minus,
            "renko_last_close": self.renko.last_close,
            "renko_direction": self.renko.direction,
            "renko_brick_count": self.renko.brick_count,
            "last_price": self.last_price,
            # position state (TTL-gated)
            "position": self.position,
            "contracts_held": self.contracts_held,
            "entry_price": self.entry_price,
            "entry_features": self.entry_features,
            "live_pnl": self.live_pnl,
            "trade_mae": self.trade_mae,
            "trade_mfe": self.trade_mfe,
            "mae_history": self.mae_history[-100:],
            "early_exit_triggered": self.early_exit_triggered,
            "had_significant_drawdown": self.had_significant_drawdown,
            "current_tp": self.current_tp,
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        """Restore from snapshot.

        Indicator state is restored UNCONDITIONALLY (no TTL) — the customer's
        complaint that "MFI recalculates on crash" is fixed by always preserving
        indicator data. After a long downtime, indicator values may be slightly
        off because the market moved while we were down, but the next few bricks
        will pull them back to current; better than re-seeding from scratch.

        Position state is TTL-gated (default 600s) to avoid acting on stale
        position bookkeeping after a long downtime — the platform may have
        flattened us. Caller (RenkoBot) re-queries the platform on startup.
        """
        # Always restore indicators
        self.brick_closes = state.get("brick_closes", [])
        self.brick_opens = state.get("brick_opens", [])
        self.brick_typicals = state.get("brick_typicals", [])
        self.brick_volumes = state.get("brick_volumes", [])
        self.ema = state.get("ema")
        self.rsg_dosc = state.get("rsg_dosc")
        self.rsg_dosc_values = state.get("rsg_dosc_values", [])
        self.renko_sma = state.get("renko_sma")
        self.mfi_value = state.get("mfi_value")
        self.prev_mfi_value = state.get("prev_mfi_value")
        # Clear MFI dots on restore — never act on a dot armed before a downtime,
        # because the customer wouldn't have seen the corresponding live bar.
        self.mfi_oversold_dot = False
        self.mfi_overbought_dot = False
        # Restore Vortex state
        self.vi_plus = state.get("vi_plus")
        self.vi_minus = state.get("vi_minus")
        self.renko.last_close = state.get("renko_last_close")
        self.renko.direction = state.get("renko_direction", 0)
        self.renko.brick_count = state.get("renko_brick_count", 0)
        self.last_price = state.get("last_price", 0.0)

        # Reconstruct missing per-brick lists if older state file had partial data
        if not self.brick_typicals and self.brick_closes and self.brick_opens:
            for i in range(len(self.brick_closes)):
                bo = self.brick_opens[i]
                bc = self.brick_closes[i]
                h = max(bo, bc)
                l = min(bo, bc)
                self.brick_typicals.append((h + l + bc) / 3.0)
        if not self.brick_volumes and self.brick_closes:
            self.brick_volumes = [1] * len(self.brick_closes)

        # Position state: only restore if recent
        position_age = time.time() - state.get("saved_at", 0)
        if position_age > position_ttl:
            print(f"  [{self.symbol}] Position state too old ({int(position_age)}s) - "
                  f"will sync from platform")
            self.position = 0
            self.contracts_held = 0
            self.entry_price = 0.0
            self.entry_features = None
        else:
            self.position = state.get("position", 0)
            self.contracts_held = state.get("contracts_held", 0)
            self.entry_price = state.get("entry_price", 0.0)
            self.entry_features = state.get("entry_features")

        self.live_pnl = state.get("live_pnl", 0.0)
        self.trade_mae = state.get("trade_mae", 0.0)
        self.trade_mfe = state.get("trade_mfe", 0.0)
        self.mae_history = state.get("mae_history", [])
        self.early_exit_triggered = state.get("early_exit_triggered", False)
        self.had_significant_drawdown = state.get("had_significant_drawdown", False)
        self.current_tp = state.get("current_tp", TP_DEFAULT_DOLLARS)
        return True

    # ----------------------------------------------------------------
    # Indicator math
    # ----------------------------------------------------------------

    _MAX_BRICK_HISTORY = 250

    def _add_brick(self, brick_open: float, brick_close: float):
        """Append a new brick and update R.sg trail. MFI/EMA computed in
        _calc_indicators() called immediately after."""
        self.brick_closes.append(brick_close)
        self.brick_opens.append(brick_open)
        vol = max(self.tick_count_this_brick, 1)
        self.brick_volumes.append(vol)
        self.tick_count_this_brick = 0
        h = max(brick_open, brick_close)
        l = min(brick_open, brick_close)
        typical = (h + l + brick_close) / 3.0
        self.brick_typicals.append(typical)
        # Update session VWAP
        self.vwap_cum_pv += typical * vol
        self.vwap_cum_vol += vol
        if self.vwap_cum_vol > 0:
            self.vwap = self.vwap_cum_pv / self.vwap_cum_vol
        self._update_rsg(brick_open, brick_close)
        if len(self.brick_closes) > self._MAX_BRICK_HISTORY:
            excess = len(self.brick_closes) - self._MAX_BRICK_HISTORY
            del self.brick_closes[:excess]
            del self.brick_opens[:excess]
            del self.brick_volumes[:excess]
            del self.brick_typicals[:excess]
            del self.rsg_dosc_values[:excess]

    def _update_rsg(self, brick_open: float, brick_close: float):
        """Renko Smoothed Gradient — a stepped trail that follows price in
        decay-sized steps. The R.sg SMA over RENKO_SMA_PERIOD bricks acts as
        a smoothed midline used by the optional distance filter."""
        hh = max(brick_open, brick_close)
        ll = min(brick_open, brick_close)
        rprice = round(brick_close / self.rsg_decay) * self.rsg_decay if self.rsg_decay > 0 else brick_close

        if self.rsg_dosc is None:
            self.rsg_dosc = rprice
        else:
            if hh > self.rsg_dosc + self.rsg_decay or ll < self.rsg_dosc - self.rsg_decay:
                self.rsg_dosc = rprice

        self.rsg_dosc_values.append(self.rsg_dosc)

    def _calc_indicators(self):
        """Recompute EMA, R.sg SMA, and MFI based on current brick history.
        Updates self.mfi_oversold_dot / mfi_overbought_dot on threshold cross."""
        n = len(self.brick_closes)
        c = self.brick_closes[-1]

        # EMA(9) for ML feature only
        if n >= EMA_PERIOD:
            if self.ema is None:
                self.ema = sum(self.brick_closes[-EMA_PERIOD:]) / EMA_PERIOD
            else:
                k = 2.0 / (EMA_PERIOD + 1)
                self.ema = c * k + self.ema * (1 - k)

        # R.sg SMA
        if len(self.rsg_dosc_values) >= RENKO_SMA_PERIOD:
            self.renko_sma = sum(self.rsg_dosc_values[-RENKO_SMA_PERIOD:]) / RENKO_SMA_PERIOD

        # MFI(14): need at least PERIOD+1 typicals
        if n >= MFI_PERIOD + 1:
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

            # Cross-detection (set dots ONCE on entry into zone)
            if self.prev_mfi_value is not None:
                if new_mfi <= MFI_OVERSOLD and self.prev_mfi_value > MFI_OVERSOLD:
                    self.mfi_oversold_dot = True
                    self.mfi_overbought_dot = False
                elif new_mfi >= MFI_OVERBOUGHT and self.prev_mfi_value < MFI_OVERBOUGHT:
                    self.mfi_overbought_dot = True
                    self.mfi_oversold_dot = False

            self.prev_mfi_value = self.mfi_value if self.mfi_value is not None else new_mfi
            self.mfi_value = new_mfi

        # Vortex Indicator(14) on Renko bricks
        if n >= VORTEX_PERIOD + 1:
            vmp = 0.0
            vmm = 0.0
            str_sum = 0.0
            for i in range(n - VORTEX_PERIOD, n):
                hi = max(self.brick_opens[i], self.brick_closes[i])
                lo = min(self.brick_opens[i], self.brick_closes[i])
                prev_hi = max(self.brick_opens[i - 1], self.brick_closes[i - 1])
                prev_lo = min(self.brick_opens[i - 1], self.brick_closes[i - 1])
                prev_c = self.brick_closes[i - 1]
                vmp += abs(hi - prev_lo)
                vmm += abs(lo - prev_hi)
                tr = max(hi - lo, abs(hi - prev_c), abs(lo - prev_c))
                str_sum += tr
            if str_sum > 0:
                self.vi_plus = vmp / str_sum
                self.vi_minus = vmm / str_sum

    # ----------------------------------------------------------------
    # Seeding from history
    # ----------------------------------------------------------------

    async def seed_history(self):
        """Fill indicator buffers from TopstepX 1sec historical bars."""
        try:
            data = await self.ctx.data.get_data("1sec", bars=1000)
        except Exception as e:
            print(f"[{self.symbol}] Historical fetch failed: {e}")
            return
        if data is None or len(data) == 0:
            print(f"[{self.symbol}] No historical 1sec data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[{self.symbol}] Seeding from {len(rows)} historical 1sec bars...")

        # Reset indicator state
        self.brick_closes.clear()
        self.brick_opens.clear()
        self.brick_typicals.clear()
        self.brick_volumes.clear()
        self.ema = None
        self.rsg_dosc = None
        self.rsg_dosc_values.clear()
        self.renko_sma = None
        self.mfi_value = None
        self.prev_mfi_value = None
        self.mfi_oversold_dot = False
        self.mfi_overbought_dot = False
        self.vi_plus = None
        self.vi_minus = None

        for row in rows:
            close = float(row["close"])
            for brick in self.renko.feed_close(close):
                self._add_brick(brick[0], brick[1])
                self._calc_indicators()

        self.mfi_oversold_dot = False
        self.mfi_overbought_dot = False

        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"
        rma_str = f"{self.renko_sma:.2f}" if self.renko_sma is not None else "N/A"
        mfi_str = f"{self.mfi_value:.2f}" if self.mfi_value is not None else "N/A"
        vi_str = f"VI+={self.vi_plus:.4f} VI-={self.vi_minus:.4f}" if self.vi_plus is not None else "VI: N/A"
        last_close_str = f"{self.renko.last_close:.2f}" if self.renko.last_close is not None else "N/A"
        print(f"  [{self.symbol}] Renko: {self.renko.brick_count} bricks, {dir_str}, ref={last_close_str}")
        print(f"  [{self.symbol}] R.sg SMA({RENKO_SMA_PERIOD}): {rma_str} | MFI({MFI_PERIOD}): {mfi_str} | {vi_str}")

    # ----------------------------------------------------------------
    # Status / freshness helpers
    # ----------------------------------------------------------------

    def print_status(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"
        dir_str = ("BULLISH" if self.renko.direction == 1
                   else "BEARISH" if self.renko.direction == -1 else "NONE")
        last_close_str = f"{self.renko.last_close:.2f}" if self.renko.last_close is not None else "N/A"
        rma_str = f"{self.renko_sma:.2f}" if self.renko_sma is not None else "N/A"
        mfi_str = f"{self.mfi_value:.2f}" if self.mfi_value is not None else "N/A"
        os_str = "ARMED" if self.mfi_oversold_dot else "no"
        ob_str = "ARMED" if self.mfi_overbought_dot else "no"

        print(f"  [{self.symbol} @ {now}]")
        print(f"    Renko: {dir_str} | last_close={last_close_str} | bricks={self.renko.brick_count}")
        print(f"    R.sg SMA: {rma_str} | MFI: {mfi_str}")
        print(f"    Oversold dot: {os_str} | Overbought dot: {ob_str}")
        if self.vi_plus is not None:
            gap = abs(self.vi_plus - self.vi_minus)
            dom = "VI+>VI-" if self.vi_plus > self.vi_minus else "VI->VI+"
            print(f"    Vortex({VORTEX_PERIOD}): VI+={self.vi_plus:.4f} VI-={self.vi_minus:.4f} | "
                  f"gap={gap:.4f} ({dom}) | threshold={VORTEX_GAP_THRESHOLD}")
        else:
            print(f"    Vortex({VORTEX_PERIOD}): warming")
        tp_str = ""
        if self.contracts_held > 0:
            tp_str = f" | TP: ${self.current_tp:.0f}"
        avg_mae_str = ""
        if self.mae_history:
            avg_mae_str = f" | Avg MAE: ${sum(self.mae_history)/len(self.mae_history):.2f} ({len(self.mae_history)} trades)"
        print(f"    Position: {pos_str} x{self.contracts_held} | P&L: ${self.live_pnl:.2f} | "
              f"PV=${self.point_value}/pt{tp_str}{avg_mae_str}")

    def _adaptive_pullback(self) -> float:
        """Compute pullback points from MAE history. Returns pts to wait."""
        if len(self.mae_history) < 5:
            return PULLBACK_DEFAULT
        avg_mae_dollars = abs(sum(self.mae_history) / len(self.mae_history))
        avg_mae_pts = avg_mae_dollars / (self.point_value * max(self.contracts_held, 1))
        pullback = avg_mae_pts * PULLBACK_MAE_RATIO
        pullback = max(PULLBACK_MIN, min(PULLBACK_MAX, pullback))
        return round(pullback * 4) / 4  # round to nearest 0.25 (NQ tick)

    def is_data_stale(self, threshold: int = 300) -> bool:
        if self.last_new_bar_time is None:
            return False
        return (time.time() - self.last_new_bar_time) > threshold

    def is_price_frozen(self, threshold: int = 180) -> bool:
        if self.last_price_change_time is None:
            return False
        return (time.time() - self.last_price_change_time) > threshold

    # ----------------------------------------------------------------
    # Position safety net (HTTP poll with divergence counter)
    # ----------------------------------------------------------------

    async def _query_platform_position(self):
        """Returns: int (signed contracts) | None (query failed).
        None means caller MUST NOT assume anything about position state."""
        if self.ctx is None:
            return None
        try:
            positions = await asyncio.wait_for(
                self.ctx.positions.get_all_positions(), timeout=4.0
            )
        except Exception as e:
            print(f"[{self.symbol}] get_all_positions error: {e}")
            return None

        if positions is None:
            return None
        if not isinstance(positions, (list, tuple)):
            return None

        cid = self.ctx.instrument_info.id
        for p in positions:
            try:
                p_cid = getattr(p, "contract_id", None) or getattr(p, "contractId", None)
                if p_cid == cid:
                    return p.signed_size
            except Exception:
                continue
        if len(positions) == 0 and self.position != 0:
            print(f"[{self.symbol}] get_all_positions returned empty while bot has position - treating as unreliable")
            return None
        return 0

    async def reconcile_position_with_platform(self):
        """Periodic safety check. Reconciles bot state with platform reality.

        Anti-flap protection: SDK's get_all_positions() has a documented bug
        where it can return [] even when positions exist. To avoid acting on
        false-flat readings, we require N consecutive flat reads
        (PLATFORM_FLAT_THRESHOLD) before assuming the position truly closed.
        """
        now_ts = time.time()
        if now_ts - self.last_position_poll_time < self.POSITION_POLL_INTERVAL:
            return
        self.last_position_poll_time = now_ts

        real_pos = await self._query_platform_position()
        if real_pos is None:
            # API failed — don't change state, don't increment streak
            return

        if self.position == 0 and real_pos == 0:
            # Both flat, all good
            self.platform_flat_streak = 0
            return

        if self.position != 0 and real_pos != 0:
            # Both in position
            if (real_pos > 0) != (self.position > 0):
                # Opposite directions — should never happen, alert
                print(f"[{self.symbol}] WARNING: bot={self.position}, platform={real_pos}")
                send_telegram(self.tg_token, self.tg_chat,
                              f"WARN|{self.symbol} state mismatch: "
                              f"bot={self.position} vs platform={real_pos}")
            self.platform_flat_streak = 0
            return

        if self.position != 0 and real_pos == 0:
            # Bot thinks open, platform says flat. Could be SDK bug, or could be
            # genuine TP/SL/manual close.
            self.platform_flat_streak += 1
            print(f"[{self.symbol}] platform shows flat (streak {self.platform_flat_streak}"
                  f"/{self.PLATFORM_FLAT_THRESHOLD})")
            if self.platform_flat_streak >= self.PLATFORM_FLAT_THRESHOLD:
                # Confirmed: platform closed our position externally
                direction = "LONG" if self.position == 1 else "SHORT"
                price = self.last_price or 0.0
                pnl_est = ((price - self.entry_price) * self.position
                           * self.point_value * max(self.contracts_held, 1)) if self.entry_price else 0.0
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"\n[{now}] [{self.symbol}] POSITION SYNC: platform closed "
                      f"{direction} externally (TP/SL/manual). Est PnL: ${pnl_est:+.2f}")

                if self.ml and self.entry_features:
                    self.ml.record_trade(self.entry_features, pnl_est, source="platform_close",
                                         entry_action=self.entry_action, mae=self.trade_mae,
                                         mfe=self.trade_mfe)
                self.entry_features = None
                self.live_pnl += pnl_est
                self._log_trade(direction, self.entry_price, price, pnl_est,
                                "PLATFORM_CLOSED", self.entry_time)

                self.position = 0
                self.contracts_held = 0
                self.entry_price = 0.0
                self.entry_time = None
                self.platform_flat_streak = 0

                send_telegram(self.tg_token, self.tg_chat,
                              f"SYNC|{self.symbol} {direction} closed externally. "
                              f"Est PnL: ${pnl_est:+.2f}")
            return

        if self.position == 0 and real_pos != 0:
            # Bot flat but platform shows position. Don't auto-act — could be
            # an order we just placed still propagating, or a stale SDK cache.
            # Just alert.
            print(f"[{self.symbol}] WARN: bot=FLAT, platform shows {real_pos} contracts")
            self.platform_flat_streak = 0

    # ----------------------------------------------------------------
    # Tick loop (called by RenkoBot every ~0.5s)
    # ----------------------------------------------------------------

    async def tick(self, cached_price=None):
        if self.ctx is None:
            return True

        if cached_price is not None:
            price = cached_price
        else:
            try:
                price = await self.ctx.data.get_current_price()
            except Exception as e:
                print(f"[{self.symbol}] get_current_price error: {e}")
                return True
            if price is None:
                return True

        # Price-change tracking for frozen-feed detection
        if self.last_known_price is None or price != self.last_known_price:
            self.last_known_price = price
            self.last_price_change_time = time.time()
            self.tick_count_this_brick += 1
        self.last_price = price

        now_ts = time.time()
        now = datetime.now(ET).strftime("%H:%M:%S")

        # Tick rate: track ticks per second over a rolling 10s window
        self.tick_window.append((now_ts, 1))
        cutoff = now_ts - 10.0
        self.tick_window = [(t, c) for t, c in self.tick_window if t >= cutoff]
        if len(self.tick_window) > 1:
            span = self.tick_window[-1][0] - self.tick_window[0][0]
            self.ticks_per_second = len(self.tick_window) / max(span, 0.1)

        # Session VWAP: reset on new trading day
        today = datetime.now(ET).date()
        if self.vwap_session_date != today:
            self.vwap_cum_pv = 0.0
            self.vwap_cum_vol = 0
            self.vwap = None
            self.vwap_session_date = today

        # Check pending pullback entry
        if self.pending_direction is not None and self.position == 0:
            elapsed = now_ts - self.pending_time
            if elapsed > PULLBACK_TIMEOUT:
                # Timeout — cancel pending and enter at market (don't miss the move)
                print(f"[{now}] [{self.symbol} PULLBACK-TIMEOUT] {self.pending_direction} "
                      f"no pullback in {PULLBACK_TIMEOUT}s, entering at market {price:.2f}")
                self.entry_features = self.pending_features
                self.entry_action = "wait"
                if self.pending_direction == "LONG":
                    await self._enter_long(price)
                else:
                    await self._enter_short(price)
                self.pending_direction = None
                self.pending_features = None
            else:
                filled = False
                if self.pending_direction == "LONG" and price <= self.pending_target_price:
                    filled = True
                elif self.pending_direction == "SHORT" and price >= self.pending_target_price:
                    filled = True
                if filled:
                    saved = self.pending_signal_price - price if self.pending_direction == "LONG" \
                        else price - self.pending_signal_price
                    print(f"[{now}] [{self.symbol} PULLBACK-FILL] {self.pending_direction} "
                          f"@ {price:.2f} (saved {saved:.1f} pts vs signal @ "
                          f"{self.pending_signal_price:.2f})")
                    self.entry_features = self.pending_features
                    self.entry_action = "wait"
                    if self.pending_direction == "LONG":
                        await self._enter_long(price)
                    else:
                        await self._enter_short(price)
                    self.pending_direction = None
                    self.pending_features = None

        # Cancel pending if a new position was opened (e.g. by opposite signal flip)
        if self.pending_direction is not None and self.position != 0:
            self.pending_direction = None
            self.pending_features = None

        # Feed renko at most once per second (matches TV 1s renko close behaviour)
        if not hasattr(self, "_last_renko_feed_time"):
            self._last_renko_feed_time = 0.0
        if now_ts - self._last_renko_feed_time >= 1.0:
            bricks = self.renko.feed_close(price)
            self._last_renko_feed_time = now_ts
        else:
            bricks = []

        # Process each brick: update indicators, then check signal IN-LOOP so a
        # multi-brick batch (price gap) can fire a signal on intermediate bricks
        # rather than only the last.
        if bricks:
            self.last_new_bar_time = now_ts
            for b in bricks:
                brick_dir = b[2]
                self._add_brick(b[0], b[1])
                self._calc_indicators()

                rma_str = f"RMA: {self.renko_sma:.2f}" if self.renko_sma is not None else "RMA: N/A"
                mfi_str = f"MFI: {self.mfi_value:.2f}" if self.mfi_value is not None else "MFI: N/A"
                color = "BULLISH" if brick_dir == 1 else "BEARISH"
                print(f"[{now}] [{self.symbol} BRICK] {color} #{self.renko.brick_count}: "
                      f"{b[0]:.2f} -> {b[1]:.2f} | {rma_str} | {mfi_str}")

                # Skip signal eval until indicators are warm
                if self.mfi_value is None:
                    continue

                # MFI dot + Vortex gap = immediate entry (NORMAL: oversold=LONG, overbought=SHORT)
                if self.mfi_oversold_dot:
                    self.mfi_oversold_dot = False
                    if self.vi_plus is not None and self.vi_minus is not None:
                        gap = self.vi_minus - self.vi_plus
                        if gap >= VORTEX_GAP_THRESHOLD:
                            print(f"[{now}] [{self.symbol} ENTRY] LONG: MFI oversold ({self.mfi_value:.1f}) "
                                  f"+ Vortex gap {gap:.4f} >= {VORTEX_GAP_THRESHOLD} "
                                  f"(VI+={self.vi_plus:.4f} VI-={self.vi_minus:.4f})")
                            await self._handle_signal("LONG", price, now, vortex_gap=gap)
                        else:
                            print(f"[{now}] [{self.symbol} SKIP] LONG: MFI oversold but "
                                  f"Vortex gap {gap:.4f} < {VORTEX_GAP_THRESHOLD}")
                    else:
                        print(f"[{now}] [{self.symbol} SKIP] LONG: MFI oversold but Vortex warming")
                elif self.mfi_overbought_dot:
                    self.mfi_overbought_dot = False
                    if self.vi_plus is not None and self.vi_minus is not None:
                        gap = self.vi_plus - self.vi_minus
                        if gap >= VORTEX_GAP_THRESHOLD:
                            print(f"[{now}] [{self.symbol} ENTRY] SHORT: MFI overbought ({self.mfi_value:.1f}) "
                                  f"+ Vortex gap {gap:.4f} >= {VORTEX_GAP_THRESHOLD} "
                                  f"(VI+={self.vi_plus:.4f} VI-={self.vi_minus:.4f})")
                            await self._handle_signal("SHORT", price, now, vortex_gap=gap)
                        else:
                            print(f"[{now}] [{self.symbol} SKIP] SHORT: MFI overbought but "
                                  f"Vortex gap {gap:.4f} < {VORTEX_GAP_THRESHOLD}")
                    else:
                        print(f"[{now}] [{self.symbol} SKIP] SHORT: MFI overbought but Vortex warming")

        # Post-exit tracking: watch price after closing to learn optimal TP
        if self.post_exit_tracking:
            elapsed = now_ts - self.post_exit_time
            if elapsed > TP_POST_EXIT_TRACK_SECS:
                # Done tracking, record result
                if self.ml and self.post_exit_features:
                    move_dollars = self.post_exit_best * self.point_value * self.qty
                    self.ml.record_post_exit(self.post_exit_features, move_dollars)
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.symbol} POST-EXIT] Price moved "
                          f"{self.post_exit_best:.2f} pts (${move_dollars:.0f}) "
                          f"in our direction after exit")
                self.post_exit_tracking = False
                self.post_exit_features = None
            else:
                # Track best move in our direction
                if self.post_exit_direction == 1:
                    move = price - self.post_exit_price
                else:
                    move = self.post_exit_price - price
                if move > self.post_exit_best:
                    self.post_exit_best = move

        # MAE/MFE tracking: track worst drawdown and best unrealized during trade
        if self.position != 0 and self.contracts_held > 0:
            contracts = self.contracts_held
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.point_value * contracts
            else:
                unrealized = (self.entry_price - price) * self.point_value * contracts
            if unrealized < self.trade_mae:
                self.trade_mae = unrealized
                if unrealized < -30:
                    self.had_significant_drawdown = True
            if unrealized > self.trade_mfe:
                self.trade_mfe = unrealized

        # Quick exit: if trade is significantly red within first 30 seconds, cut it
        if (self.position != 0 and self.contracts_held > 0 and self.entry_time):
            time_in_trade = (datetime.now(ET) - self.entry_time).total_seconds()
            if time_in_trade <= QUICK_EXIT_SECS:
                contracts = self.contracts_held
                if self.position == 1:
                    unrealized = (price - self.entry_price) * self.point_value * contracts
                else:
                    unrealized = (self.entry_price - price) * self.point_value * contracts
                if unrealized <= -QUICK_EXIT_DOLLARS:
                    direction = "LONG" if self.position == 1 else "SHORT"
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.symbol} QUICK-EXIT] {direction} -${abs(unrealized):.0f} "
                          f"in {time_in_trade:.0f}s — clean trades don't do this")
                    if self.ml and self.entry_features:
                        self.ml.record_trade(self.entry_features, unrealized, source="quick_exit",
                                             entry_action=self.entry_action,
                                             mae=self.trade_mae, mfe=self.trade_mfe)
                        self.entry_features = None
                    await self._flatten(price, reason="QUICK_EXIT")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"QUICK-EXIT|{self.symbol} {direction} @ {price:.2f} | "
                                  f"${unrealized:+.0f} in {time_in_trade:.0f}s")

        # Smart early exit: cut trade if MAE exceeds what winners typically see
        if (self.position != 0 and self.contracts_held > 0
                and not self.early_exit_triggered and self.ml
                and self.entry_features and self.entry_time):
            time_in_trade = (datetime.now(ET) - self.entry_time).total_seconds()
            if time_in_trade >= EARLY_EXIT_COOLDOWN:
                should_exit, exit_reason, avg_win = self.ml.should_early_exit(
                    self.entry_features, self.trade_mae)
                if should_exit:
                    self.early_exit_triggered = True
                    direction = "LONG" if self.position == 1 else "SHORT"
                    contracts = self.contracts_held
                    trade_pnl = ((price - self.entry_price) * self.position
                                 * self.point_value * contracts)
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.symbol} EARLY-EXIT] {direction} pattern failed! "
                          f"{exit_reason} | PnL: ${trade_pnl:+.2f}")
                    if self.ml and self.entry_features:
                        self.ml.record_trade(self.entry_features, trade_pnl,
                                             source="early_exit",
                                             entry_action=self.entry_action,
                                             mae=self.trade_mae, mfe=self.trade_mfe)
                        self.entry_features = None
                    await self._flatten(price, reason="EARLY_EXIT")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"EARLY-EXIT|{self.symbol} {direction} cut @ {price:.2f} | "
                                  f"PnL ${trade_pnl:+.0f} | pattern failed")

        # Hard max stop loss: absolute ceiling to prevent catastrophic losses
        if self.position != 0 and self.contracts_held > 0:
            contracts = self.contracts_held
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.point_value * contracts
            else:
                unrealized = (self.entry_price - price) * self.point_value * contracts
            if unrealized <= -MAX_LOSS_DOLLARS:
                direction = "LONG" if self.position == 1 else "SHORT"
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] [{self.symbol} MAX-SL] {direction} hit ${MAX_LOSS_DOLLARS:.0f} "
                      f"max loss! Unrealized: ${unrealized:.2f}")
                if self.ml and self.entry_features:
                    self.ml.record_trade(self.entry_features, unrealized, source="max_sl",
                                         entry_action=self.entry_action,
                                         mae=self.trade_mae, mfe=self.trade_mfe)
                    self.entry_features = None
                await self._flatten(price, reason="MAX_SL")
                send_telegram(self.tg_token, self.tg_chat,
                              f"MAX-SL|{self.symbol} {direction} cut @ {price:.2f} | "
                              f"${unrealized:+.0f} hit max loss ceiling")

        # Breakeven exit: if trade dipped and recovered but state says unlikely to hit TP
        if (self.position != 0 and self.contracts_held > 0
                and self.had_significant_drawdown and self.ml
                and self.entry_features and self.entry_time):
            time_in_trade = (datetime.now(ET) - self.entry_time).total_seconds()
            if time_in_trade >= BE_EXIT_MIN_TIME:
                contracts = self.contracts_held
                if self.position == 1:
                    current_pnl = (price - self.entry_price) * self.point_value * contracts
                else:
                    current_pnl = (self.entry_price - price) * self.point_value * contracts
                should_be, be_reason = self.ml.should_breakeven_exit(
                    self.entry_features, current_pnl, self.had_significant_drawdown)
                if should_be:
                    direction = "LONG" if self.position == 1 else "SHORT"
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.symbol} BE-EXIT] {direction} taking breakeven! "
                          f"{be_reason} | PnL: ${current_pnl:+.2f}")
                    if self.ml and self.entry_features:
                        self.ml.record_trade(self.entry_features, current_pnl,
                                             source="be_exit",
                                             entry_action=self.entry_action,
                                             mae=self.trade_mae, mfe=self.trade_mfe)
                        self.entry_features = None
                    await self._flatten(price, reason="BE_EXIT")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"BE-EXIT|{self.symbol} {direction} @ {price:.2f} | "
                                  f"PnL ${current_pnl:+.0f} | took breakeven")

        # Take profit check (dynamic TP)
        if self.position != 0 and self.contracts_held > 0:
            contracts = self.contracts_held
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.point_value * contracts
            else:
                unrealized = (self.entry_price - price) * self.point_value * contracts
            if unrealized >= self.current_tp:
                direction = "LONG" if self.position == 1 else "SHORT"
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] [{self.symbol} TP] ${self.current_tp:.0f} target hit! "
                      f"{direction} x{contracts} | Unrealized: ${unrealized:.2f}")
                if self.ml and self.entry_features:
                    self.ml.record_trade(self.entry_features, unrealized, source="tp_hit",
                                         entry_action=self.entry_action,
                                         mae=self.trade_mae, mfe=self.trade_mfe)
                    self.entry_features = None
                await self._flatten(price, reason="TP_HIT")
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "FLAT", self.symbol, price, 0),
                    kwargs={"ntfy_topic": self.ntfy_topic,
                            "tp_webhooks": self.tp_webhooks}, daemon=True).start()

        return True

    async def _handle_signal(self, direction: str, price: float, now: str, vortex_gap: float = 0.0):
        """Fire entry/flip logic for a confirmed MFI+brick signal."""
        # R.sg distance filter (optional)
        if self.use_rsg_filter and self.renko_sma is not None:
            last_bc = self.brick_closes[-1] if self.brick_closes else price
            if abs(last_bc - self.renko_sma) <= self.brick_size:
                print(f"[{now}] [{self.symbol} RSG-FILTER] {direction} skipped: brick_close "
                      f"{last_bc:.2f} too close to R.sg {self.renko_sma:.2f} "
                      f"(<= {self.brick_size:.2f} pts)")
                return

        # If already in position, skip same-direction or flip opposite
        if self.position != 0:
            already_dir = "LONG" if self.position == 1 else "SHORT"
            if (direction == "LONG" and self.position == 1) or (direction == "SHORT" and self.position == -1):
                print(f"[{now}] [{self.symbol}] {direction} signal: already {already_dir} x{self.contracts_held}, skipping")
                return

            # Opposite-side flip
            print(f"[{now}] [{self.symbol} FLIP] {already_dir} x{self.contracts_held} -> {direction}: closing first")
            trade_pnl = ((price - self.entry_price) * self.position
                         * self.point_value * self.contracts_held)
            if self.ml and self.entry_features:
                self.ml.record_trade(self.entry_features, trade_pnl, source="signal_flip",
                                     entry_action=self.entry_action, mae=self.trade_mae,
                                     mfe=self.trade_mfe)
                self.entry_features = None
            await self._flatten(price, reason="SIGNAL_FLIP")
            if self.position != 0:
                print(f"[{now}] [{self.symbol}] flatten failed, aborting flip")
                return

        # Build RL features and ask filter
        mfi_velocity = (self.mfi_value - self.prev_mfi_value) if self.prev_mfi_value is not None else 0.0
        features = self.ml.extract_features(
            direction=1 if direction == "LONG" else -1,
            mfi_value=self.mfi_value,
            mfi_velocity=mfi_velocity,
            price=price,
            ema=self.ema,
            rsg=self.renko_sma,
            brick_size=self.brick_size,
            brick_closes=self.brick_closes,
            vortex_gap=vortex_gap,
            vwap=self.vwap,
            tick_rate=self.ticks_per_second,
        ) if self.ml else None

        if features and self.ml:
            action_str, _, reason = self.ml.should_skip(features)
            if action_str == "skip":
                print(f"[{now}] [{self.symbol} RL-SKIP] {direction} skipped | {reason}")
                send_telegram(self.tg_token, self.tg_chat,
                              f"RL-SKIP|{self.symbol} {direction} @ {price:.2f} | {reason}")
                return
            if action_str == "flip":
                # RL thinks opposite direction is better
                flipped = "SHORT" if direction == "LONG" else "LONG"
                print(f"[{now}] [{self.symbol} RL-FLIP] {direction} -> {flipped} | {reason}")
                send_telegram(self.tg_token, self.tg_chat,
                              f"RL-FLIP|{self.symbol} {direction}->{flipped} @ {price:.2f}")
                self.entry_features = features
                self.entry_action = "flip"
                if flipped == "LONG":
                    await self._enter_long(price)
                else:
                    await self._enter_short(price)
                return
            if action_str == "wait":
                # Set pending pullback entry (adaptive from MAE)
                pb_pts = self._adaptive_pullback()
                if direction == "LONG":
                    target = price - pb_pts
                else:
                    target = price + pb_pts
                self.pending_direction = direction
                self.pending_signal_price = price
                self.pending_target_price = target
                self.pending_time = time.time()
                self.pending_features = features
                print(f"[{now}] [{self.symbol} RL-WAIT] {direction} waiting for pullback "
                      f"to {target:.2f} ({pb_pts} pts, adaptive) | {reason}")
                send_telegram(self.tg_token, self.tg_chat,
                              f"RL-WAIT|{self.symbol} {direction} @ {price:.2f} -> target {target:.2f}")
                return
            print(f"[{now}] [{self.symbol} RL-OK] {direction} enter now | {reason}")
        self.entry_features = features
        self.entry_action = "enter"

        # Place the entry immediately
        if direction == "LONG":
            await self._enter_long(price)
        else:
            await self._enter_short(price)

    # ----------------------------------------------------------------
    # Order placement (shielded against task cancellation)
    # ----------------------------------------------------------------

    async def _ensure_flat_before_entry(self):
        """Defensive close — runs before an entry to clear any orphan position
        the bot may not know about. Cheap if already flat."""
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=4.0)
        except Exception:
            pass

    async def _cleanup_ghost_position(self):
        """Called after an order timeout/error: best-effort close in case the
        order actually filled despite our error response."""
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=4.0)
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [{self.symbol}] Ghost-position cleanup attempted")
        except Exception:
            pass

    async def _enter_long(self, price: float):
        if self.position != 0:
            print(f"[{self.symbol}] BLOCKED LONG: already in position")
            return False
        await self._ensure_flat_before_entry()
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG @ {price:.2f} | "
              f"Session P&L: ${self.live_pnl:.2f}")

        async def _do_order():
            try:
                response = await asyncio.wait_for(
                    self.ctx.orders.place_market_order(
                        contract_id=self.ctx.instrument_info.id,
                        side=0, size=self.qty),
                    timeout=15.0)
                if response.success:
                    self.position = 1
                    self.contracts_held = self.qty
                    self.entry_price = price
                    self.entry_time = datetime.now(ET)
                    self.trade_mae = 0.0
                    self.trade_mfe = 0.0
                    self.early_exit_triggered = False
                    self.had_significant_drawdown = False
                    if self.ml and self.entry_features:
                        self.current_tp, tp_reason = self.ml.get_dynamic_tp(
                            self.entry_features, self.TP_DOLLARS)
                        print(f"[{self.symbol}] TP set: ${self.current_tp:.0f} ({tp_reason})")
                    else:
                        self.current_tp = self.TP_DOLLARS
                    print(f"[{self.symbol}] Order filled. ID: {response.orderId}")
                    return "ok"
                print(f"[{self.symbol}] Order REJECTED: {response.errorMessage}")
                return "rejected"
            except asyncio.TimeoutError:
                print(f"[{self.symbol}] Order TIMEOUT (15s)")
                return "timeout"
            except Exception as ex:
                print(f"[{self.symbol}] Order ERROR: {ex}")
                return "error"

        result = await asyncio.shield(_do_order())
        if result == "error":
            await self._cleanup_ghost_position()
            print(f"[{self.symbol}] Retrying LONG order after error...")
            await asyncio.sleep(1.0)
            result = await asyncio.shield(_do_order())
        self.last_order_error = result if result != "ok" else None

        if result == "ok":
            threading.Thread(target=send_signals, args=(
                self.tg_token, self.tg_chat, self.tg_keys,
                "LONG", self.symbol, price, self.contracts_held),
                kwargs={"ntfy_topic": self.ntfy_topic,
                        "tp_webhooks": self.tp_webhooks}, daemon=True).start()
            return True
        if result == "rejected":
            send_telegram(self.tg_token, self.tg_chat,
                          f"ALERT|{self.symbol} LONG REJECTED @ {price:.2f}")
            return False
        await self._cleanup_ghost_position()
        send_telegram(self.tg_token, self.tg_chat,
                      f"ALERT|{self.symbol} LONG {result.upper()} @ {price:.2f}")
        return False

    async def _enter_short(self, price: float):
        if self.position != 0:
            print(f"[{self.symbol}] BLOCKED SHORT: already in position")
            return False
        await self._ensure_flat_before_entry()
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT @ {price:.2f} | "
              f"Session P&L: ${self.live_pnl:.2f}")

        async def _do_order():
            try:
                response = await asyncio.wait_for(
                    self.ctx.orders.place_market_order(
                        contract_id=self.ctx.instrument_info.id,
                        side=1, size=self.qty),
                    timeout=15.0)
                if response.success:
                    self.position = -1
                    self.contracts_held = self.qty
                    self.entry_price = price
                    self.entry_time = datetime.now(ET)
                    self.trade_mae = 0.0
                    self.trade_mfe = 0.0
                    self.early_exit_triggered = False
                    self.had_significant_drawdown = False
                    if self.ml and self.entry_features:
                        self.current_tp, tp_reason = self.ml.get_dynamic_tp(
                            self.entry_features, self.TP_DOLLARS)
                        print(f"[{self.symbol}] TP set: ${self.current_tp:.0f} ({tp_reason})")
                    else:
                        self.current_tp = self.TP_DOLLARS
                    print(f"[{self.symbol}] Order filled. ID: {response.orderId}")
                    return "ok"
                print(f"[{self.symbol}] Order REJECTED: {response.errorMessage}")
                return "rejected"
            except asyncio.TimeoutError:
                print(f"[{self.symbol}] Order TIMEOUT (15s)")
                return "timeout"
            except Exception as ex:
                print(f"[{self.symbol}] Order ERROR: {ex}")
                return "error"

        result = await asyncio.shield(_do_order())
        if result == "error":
            await self._cleanup_ghost_position()
            print(f"[{self.symbol}] Retrying SHORT order after error...")
            await asyncio.sleep(1.0)
            result = await asyncio.shield(_do_order())
        self.last_order_error = result if result != "ok" else None

        if result == "ok":
            threading.Thread(target=send_signals, args=(
                self.tg_token, self.tg_chat, self.tg_keys,
                "SHORT", self.symbol, price, self.contracts_held),
                kwargs={"ntfy_topic": self.ntfy_topic,
                        "tp_webhooks": self.tp_webhooks}, daemon=True).start()
            return True
        if result == "rejected":
            send_telegram(self.tg_token, self.tg_chat,
                          f"ALERT|{self.symbol} SHORT REJECTED @ {price:.2f}")
            return False
        await self._cleanup_ghost_position()
        send_telegram(self.tg_token, self.tg_chat,
                      f"ALERT|{self.symbol} SHORT {result.upper()} @ {price:.2f}")
        return False

    async def _flatten(self, price: float, reason: str = ""):
        """Close current position. Bookkeeping happens AFTER close confirms,
        and the close call is shielded from cancellation."""
        if self.position == 0:
            return True

        direction = "LONG" if self.position == 1 else "SHORT"
        saved_entry_price = self.entry_price
        saved_position = self.position
        saved_contracts = max(self.contracts_held, 1)
        trade_pnl = ((price - saved_entry_price) * saved_position
                     * self.point_value * saved_contracts)
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] <<< EXITING {direction} x{saved_contracts} @ {price:.2f} | "
              f"Trade: ${trade_pnl:+.2f} | Session est: ${self.live_pnl + trade_pnl:.2f} | {reason}")

        async def _do_close():
            try:
                await asyncio.wait_for(
                    self.ctx.positions.close_position_direct(
                        contract_id=self.ctx.instrument_info.id),
                    timeout=5.0)
                print(f"[{self.symbol}] Closed via close_position_direct")
                return True
            except asyncio.TimeoutError:
                print(f"[{self.symbol}] close TIMEOUT — assuming TP/SL closed it")
                return True
            except Exception as ex:
                print(f"[{self.symbol}] close failed ({ex}) — assuming closed externally")
                return True

        close_ok = await asyncio.shield(_do_close())

        if close_ok:
            saved_entry_time = self.entry_time
            saved_mae = self.trade_mae
            saved_mfe = self.trade_mfe
            self.mae_history.append(saved_mae)
            if len(self.mae_history) > 100:
                self.mae_history = self.mae_history[-100:]
            avg_mae = sum(self.mae_history) / len(self.mae_history)
            now_t = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_t}] [{self.symbol} MAE/MFE] MAE: ${saved_mae:.2f} | "
                  f"MFE: ${saved_mfe:.2f} | Avg MAE: ${avg_mae:.2f}")
            # Start post-exit tracking to learn if we're leaving money on table
            self.post_exit_tracking = True
            self.post_exit_direction = saved_position
            self.post_exit_price = price
            self.post_exit_time = time.time()
            self.post_exit_best = 0.0
            self.post_exit_features = self.entry_features
            self.live_pnl += trade_pnl
            self.position = 0
            self.contracts_held = 0
            self.entry_price = 0.0
            self.entry_time = None
            self.trade_mae = 0.0
            self.trade_mfe = 0.0
            self._log_trade(direction, saved_entry_price, price, trade_pnl, reason,
                            saved_entry_time, mae=saved_mae)
        return close_ok

    def _log_trade(self, direction, entry_price, exit_price, pnl, reason, entry_time=None, mae=0.0):
        now = datetime.now(ET)
        et_str = "N/A"
        if entry_time is not None:
            et_str = entry_time.strftime("%H:%M:%S")
        elif self.entry_time is not None:
            et_str = self.entry_time.strftime("%H:%M:%S")
        trade = {
            "date": now.strftime("%Y-%m-%d"),
            "symbol": self.symbol,
            "entry_time": et_str,
            "exit_time": now.strftime("%H:%M:%S"),
            "direction": direction,
            "entry": entry_price,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,
            "mfi": self.mfi_value,
            "rsg_sma": self.renko_sma,
            "ema": self.ema,
            "mae": mae,
            "avg_mae": sum(self.mae_history) / len(self.mae_history) if self.mae_history else 0.0,
            "account": os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown"),
            "session_pnl": self.live_pnl,
        }
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(trade) + "\n")
        except Exception as e:
            print(f"[{self.symbol}] Trade log write error: {e}")


# ============================================================
# Main Bot (connection, session, multi-symbol orchestration)
# ============================================================

class RenkoBot:
    def __init__(self, symbol_configs: list, tg_token: str = "", tg_chat: str = "",
                 tg_keys: list = None, tick_interval: int = 1,
                 use_rsg_filter: bool = False, tp_webhooks: list = None):
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tp_webhooks = tp_webhooks or []
        self.tick_interval = tick_interval

        ml_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rl_state.json")
        self.ml = TradeML(ml_file)

        self.states = {}
        for cfg in symbol_configs:
            sym = cfg["symbol"]
            state = SymbolState(
                symbol=sym,
                brick_size=cfg["brick_size"],
                qty=cfg["qty"],
                ntfy_topic=cfg.get("ntfy_topic", ""),
                tg_token=tg_token,
                tg_chat=tg_chat,
                tg_keys=self.tg_keys,
                tick_interval=tick_interval,
                use_rsg_filter=use_rsg_filter,
                tp_webhooks=self.tp_webhooks,
            )
            state.ml = self.ml
            self.states[sym] = state

        self.was_in_session = False
        self.last_price_time = None
        self.connection_alive = True
        self.disconnect_alert_sent = False
        self.STALE_THRESHOLD = 120
        self.RECONNECT_THRESHOLD = 120

        # Reconnect state with exponential backoff
        self.reconnecting = False
        self.last_reconnect_time = 0
        self.reconnect_failures = 0
        self.reconnect_cooldown = 5
        self.RECONNECT_COOLDOWN_OK = 30
        self.RECONNECT_COOLDOWN_MAX = 120

        # WS event handler de-dupe (avoid double-fire when SDK emits same event
        # under multiple names — we keep the most recent event per contract).
        self._ws_close_seen = {}  # contract_id -> last_seen_ts

        self.last_status_notify = 0
        self.last_state_save = 0
        self.last_heartbeat = 0
        self.HEARTBEAT_INTERVAL = 1800
        self.last_gateway_logout_time = 0
        self.last_gc_time = 0
        self.GC_INTERVAL = 300

        self.suite = None
        self.running = False
        self.state_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "bot_state.json"
        )
        self._watchdog_tick = time.time()
        self.WATCHDOG_TIMEOUT = 300

    def _symbols_list(self):
        return list(self.states.keys())

    # ----------------------------------------------------------------
    # State persistence
    # ----------------------------------------------------------------

    def save_all_state(self):
        try:
            state = {sym: st.save_state() for sym, st in self.states.items()}
            tmp_file = self.state_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f)
            # Atomic replace prevents partial writes if interrupted
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            print(f"[BOT] save_all_state error: {e}")

    def load_all_state(self) -> bool:
        try:
            if not os.path.exists(self.state_file):
                return False
            with open(self.state_file) as f:
                saved = json.load(f)
            restored_any = False
            for sym, st in self.states.items():
                if sym in saved:
                    if st.restore_state(saved[sym]):
                        ema_s = f"{st.ema:.2f}" if st.ema is not None else "N/A"
                        mfi_s = f"{st.mfi_value:.2f}" if st.mfi_value is not None else "N/A"
                        print(f"  [{sym}] Restored: bricks={st.renko.brick_count}, "
                              f"EMA={ema_s}, MFI={mfi_s}")
                        restored_any = True
            return restored_any
        except Exception as e:
            print(f"[BOT] load_all_state error: {e}")
            return False

    def _notify_status(self, msg):
        """Rate-limited status telegram (max 1 per 5 min)."""
        now_ts = time.time()
        if now_ts - self.last_status_notify > 300:
            send_telegram(self.tg_token, self.tg_chat, msg)
            self.last_status_notify = now_ts

    # ----------------------------------------------------------------
    # Main run loop
    # ----------------------------------------------------------------

    async def run(self):
        from project_x_py import TradingSuite

        symbols = self._symbols_list()
        print(f"[BOT] Renko MFI + Vortex Gap Strategy - LIVE MODE")
        print(f"[BOT] Symbols: {', '.join(symbols)}")
        for sym, st in self.states.items():
            print(f"[BOT]   {sym}: brick={st.brick_size}, qty={st.qty}, "
                  f"pv=${st.point_value}/pt"
                  + (f", ntfy={st.ntfy_topic}" if st.ntfy_topic else ""))
        print(f"[BOT] ENTRY: MFI dot ({MFI_OVERSOLD}/{MFI_OVERBOUGHT}) + "
              f"Vortex({VORTEX_PERIOD}) gap >= {VORTEX_GAP_THRESHOLD} → enter immediately")
        print(f"[BOT] EXIT: opposite MFI signal flips position or TP hit")
        rsg_state = "ENABLED" if any(s.use_rsg_filter for s in self.states.values()) else "disabled"
        print(f"[BOT] R.sg distance filter: {rsg_state}")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - "
              f"{SESSION_END.strftime('%H:%M')} ET ({trading_day_str})")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        if self.ml:
            print(f"[BOT] RL filter: {self.ml.stats()}")
            print(f"[BOT] RL settings: warmup={RL_WARMUP_TRADES}, alpha={RL_ALPHA}, "
                  f"gamma={RL_GAMMA}, eps_start={RL_EPSILON_START}, eps_min={RL_EPSILON_MIN}")
        else:
            print("[BOT] RL filter: DISABLED")
        if self.tp_webhooks:
            print(f"[BOT] TradersPost webhooks: {len(self.tp_webhooks)} configured")
        print()

        self.suite = await TradingSuite.create(
            instruments=symbols,
            timeframes=["1sec", "15min"],
            initial_days=1,
        )
        self._register_websocket_handlers()

        print(f"[BOT] Connected to TopstepX")
        try:
            print(f"[BOT] Account: {self.suite.client.account_info.name}")
        except Exception:
            pass

        restored = self.load_all_state()

        for sym, st in self.states.items():
            st.ctx = self.suite[sym]
            print(f"[BOT] {sym} contract: {st.ctx.instrument_info.id}")
            try:
                price = await st.ctx.data.get_current_price()
            except Exception:
                price = None
            if price:
                if not restored or st.renko.last_close is None:
                    st.renko.initialize(price)
                st.last_price = price
                print(f"[BOT] {sym} price: {price:.2f}")
            if not restored or not st.brick_closes:
                await st.seed_history()
            else:
                print(f"  [{sym}] Using restored indicator state (skipping seed)")

        # On startup, reconcile every symbol with platform reality once.
        # This catches the "bot crashed while in position" case.
        for sym, st in self.states.items():
            if st.ctx is None:
                continue
            real_pos = await st._query_platform_position()
            if real_pos is None:
                print(f"  [{sym}] Could not query platform position on startup")
                continue
            if real_pos == 0 and st.position != 0:
                print(f"  [{sym}] Bot thought {st.position}, platform flat — resetting state")
                send_telegram(self.tg_token, self.tg_chat,
                              f"STARTUP|{sym} bot thought in position, platform flat — reset")
                st.position = 0
                st.contracts_held = 0
                st.entry_price = 0.0
                st.entry_features = None
            elif real_pos != 0 and st.position == 0:
                print(f"  [{sym}] Bot thought flat, platform shows {real_pos} — closing it")
                send_telegram(self.tg_token, self.tg_chat,
                              f"STARTUP|{sym} unknown {real_pos}-contract position — closing")
                try:
                    await asyncio.wait_for(
                        st.ctx.positions.close_position_direct(
                            contract_id=st.ctx.instrument_info.id),
                        timeout=5.0)
                except Exception as e:
                    print(f"  [{sym}] startup close failed: {e}")
            elif real_pos != 0 and st.position != 0:
                if (real_pos > 0) != (st.position > 0):
                    print(f"  [{sym}] OPPOSITE direction! bot={st.position} platform={real_pos}")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"CRITICAL|{sym} bot {st.position} vs platform {real_pos}")

        print()
        self.running = True
        self.was_in_session = in_session()
        for st in self.states.values():
            st.print_status()
        print(f"\n[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Trading LIVE - MFI+Vortex strategy ({', '.join(symbols)})")
        print(f"[BOT] Watchdog: {self.WATCHDOG_TIMEOUT}s timeout")
        print(f"[BOT] Press Ctrl+C to stop\n")

        def _watchdog():
            while self.running:
                time.sleep(30)
                elapsed = time.time() - self._watchdog_tick
                if elapsed > self.WATCHDOG_TIMEOUT and in_session():
                    print(f"[WATCHDOG] Main loop stuck for {int(elapsed)}s — killing process")
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(5)
                    os._exit(1)
        threading.Thread(target=_watchdog, daemon=True).start()

        try:
            while self.running:
                try:
                    self._watchdog_tick = time.time()
                    await self._tick()
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    if not self.running:
                        break
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [WARN] Task cancelled (GatewayLogout?) - reconnecting...")
                    try:
                        await self._auto_reconnect()
                    except (asyncio.CancelledError, Exception) as re:
                        print(f"[{now}] [WARN] Reconnect failed: {re}")
                        try:
                            await asyncio.sleep(5)
                        except asyncio.CancelledError:
                            pass
                except Exception as e:
                    if not self.running:
                        break
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [WARN] Tick error: {e} - reconnecting...")
                    try:
                        await self._auto_reconnect()
                    except (asyncio.CancelledError, Exception) as re:
                        print(f"[{now}] [WARN] Reconnect failed: {re}")
                        try:
                            await asyncio.sleep(5)
                        except asyncio.CancelledError:
                            pass
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    # ----------------------------------------------------------------
    # WebSocket handlers (gateway logout + position updates)
    # ----------------------------------------------------------------

    def _register_websocket_handlers(self):
        try:
            conn = self.suite.realtime.user_connection
        except Exception as e:
            print(f"[WARN] Could not access user_connection: {e}")
            return

        def on_logout(*args):
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [LOGOUT] GatewayLogout received")
            self.last_gateway_logout_time = time.time()

        def on_position_event(*args):
            """De-duped position-update handler. The SDK can emit the same event
            under multiple names; we collapse them via _ws_close_seen."""
            for arg in args:
                try:
                    cid = (getattr(arg, "contract_id", None)
                           or getattr(arg, "contractId", None))
                    size = float(getattr(arg, "size", 0)
                                 or getattr(arg, "net_pos", 0) or 0)
                    if cid is None:
                        continue
                    if abs(size) >= 0.01:
                        # Position still open — not interesting
                        continue
                    # Position closed event — de-dupe within 2s
                    last_seen = self._ws_close_seen.get(cid, 0)
                    now_ts = time.time()
                    if now_ts - last_seen < 2.0:
                        continue
                    self._ws_close_seen[cid] = now_ts

                    # Find matching symbol
                    for sym, st in self.states.items():
                        if (st.ctx is None
                                or st.ctx.instrument_info.id != cid):
                            continue
                        if st.position == 0:
                            # Bot already flat — nothing to reconcile
                            continue
                        direction = "LONG" if st.position == 1 else "SHORT"
                        price = st.last_price or 0.0
                        pnl_est = ((price - st.entry_price) * st.position
                                   * st.point_value * max(st.contracts_held, 1)) if st.entry_price else 0.0
                        ts = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"\n[{ts}] [{sym}] WS-SYNC: {direction} closed externally. "
                              f"Est PnL: ${pnl_est:+.2f}")

                        if st.ml and st.entry_features:
                            st.ml.record_trade(st.entry_features, pnl_est,
                                               source="ws_platform_close",
                                               entry_action=st.entry_action,
                                               mae=st.trade_mae, mfe=st.trade_mfe)
                        st.entry_features = None
                        st.live_pnl += pnl_est
                        st._log_trade(direction, st.entry_price, price, pnl_est,
                                      "WS_PLATFORM_CLOSED", st.entry_time)
                        st.position = 0
                        st.contracts_held = 0
                        st.entry_price = 0.0
                        st.entry_time = None
                        st.platform_flat_streak = 0

                        threading.Thread(target=send_telegram, args=(
                            self.tg_token, self.tg_chat,
                            f"WS-SYNC|{sym} {direction} closed externally. "
                            f"Est PnL: ${pnl_est:+.2f}"), daemon=True).start()
                        threading.Thread(target=send_signals, args=(
                            self.tg_token, self.tg_chat, self.tg_keys,
                            "FLAT", sym, price, 0),
                            kwargs={"ntfy_topic": st.ntfy_topic,
                                    "tp_webhooks": st.tp_webhooks}, daemon=True).start()
                except Exception:
                    continue

        # Register both events to the same handler (deduped above)
        registered = []
        try:
            conn.on("GatewayLogout", on_logout); registered.append("GatewayLogout")
        except Exception as e:
            print(f"[WARN] GatewayLogout register failed: {e}")
        try:
            conn.on("GatewayUserPosition", on_position_event); registered.append("GatewayUserPosition")
        except Exception as e:
            print(f"[WARN] GatewayUserPosition register failed: {e}")
        try:
            conn.on("PositionUpdate", on_position_event); registered.append("PositionUpdate")
        except Exception as e:
            print(f"[WARN] PositionUpdate register failed: {e}")

        if registered:
            print(f"[BOT] WS handlers registered: {', '.join(registered)}")
        else:
            print(f"[BOT] WARNING: no WS handlers registered — relying on HTTP poll only")

    # ----------------------------------------------------------------
    # Reconnect with exponential backoff
    # ----------------------------------------------------------------

    async def _auto_reconnect(self):
        from project_x_py import TradingSuite
        self.reconnecting = True
        attempt_start = time.time()
        self.last_reconnect_time = attempt_start
        now = datetime.now(ET).strftime("%H:%M:%S")
        symbols = self._symbols_list()
        print(f"[{now}] [RECONNECT] attempt #{self.reconnect_failures + 1}, indicators preserved...")
        self._notify_status(f"STATUS|Auto-reconnecting ({now} ET)")

        if self.suite:
            try:
                await asyncio.wait_for(self.suite.disconnect(), timeout=10.0)
            except Exception:
                pass

        try:
            self.suite = await asyncio.wait_for(
                TradingSuite.create(
                    instruments=symbols,
                    timeframes=["1sec", "15min"],
                    initial_days=1,
                ),
                timeout=60.0,
            )
            for sym, st in self.states.items():
                st.ctx = self.suite[sym]
                # Fresh connection — reset freshness trackers, NOT indicators
                st.last_known_price = None
                st.last_price_change_time = time.time()
                st.last_new_bar_time = time.time()
                st.platform_flat_streak = 0  # reset poll streak

            self._register_websocket_handlers()
            self.last_price_time = time.time()
            self.connection_alive = True
            self.disconnect_alert_sent = False

            # Success: reset failure counter, normal cooldown
            self.reconnect_failures = 0
            self.reconnect_cooldown = self.RECONNECT_COOLDOWN_OK

            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] WebSocket restored")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|RECONNECTED ({now} ET)")

            # Keep position open through reconnects — no safety flatten.
            # Bot preserves all indicator/BB state so it can detect
            # new averaging-in or exit signals immediately after reconnect.
            for sym, st in self.states.items():
                if st.position != 0:
                    direction = "LONG" if st.position == 1 else "SHORT"
                    print(f"[{now}] [RECONNECT] {sym} still {direction} x{st.contracts_held} — keeping position")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"STATUS|{sym} still {direction} x{st.contracts_held} after reconnect ({now} ET)")

        except asyncio.TimeoutError:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] TIMEOUT (60s) — forcing clean restart")
            self._notify_status(f"STATUS|Reconnect timeout, restarting ({now} ET)")
            self.save_all_state()
            self.suite = None
            self.reconnecting = False
            raise RuntimeError("Reconnect timeout — forcing restart")
        except Exception as e:
            self.reconnect_failures += 1
            self.reconnect_cooldown = min(
                5 * (2 ** (self.reconnect_failures - 1)),
                self.RECONNECT_COOLDOWN_MAX,
            )
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] Failed (#{self.reconnect_failures}): {e} — "
                  f"retry in {self.reconnect_cooldown}s")
            self.suite = None
            for st in self.states.values():
                st.ctx = None
        finally:
            self.reconnecting = False

    # ----------------------------------------------------------------
    # Main tick (multi-symbol orchestration)
    # ----------------------------------------------------------------

    async def _tick(self):
        if self.suite is None:
            if in_session() and not self.reconnecting:
                if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
                    await self._auto_reconnect()
            return

        # Multi-symbol health check — don't rely on first symbol only
        now_ts = time.time()
        any_price_ok = False
        any_price_frozen = False
        frozen_sym = None
        cached_prices = {}
        for sym, st in self.states.items():
            if st.ctx is None:
                continue
            try:
                p = await asyncio.wait_for(
                    st.ctx.data.get_current_price(), timeout=5.0)
            except Exception:
                p = None
            if p is not None:
                cached_prices[sym] = p
                any_price_ok = True
                if st.last_known_price is None or p != st.last_known_price:
                    st.last_known_price = p
                    st.last_price_change_time = now_ts
                if st.is_price_frozen(threshold=180):
                    any_price_frozen = True
                    frozen_sym = sym

        if not any_price_ok:
            if self.last_price_time and in_session():
                elapsed = now_ts - self.last_price_time
                if elapsed > self.STALE_THRESHOLD and not self.disconnect_alert_sent:
                    self.connection_alive = False
                    self.disconnect_alert_sent = True
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [ALERT] No price data on any symbol for {int(elapsed)}s")
                    self._notify_status(f"STATUS|DISCONNECTED ({now} ET)")
                if elapsed > self.RECONNECT_THRESHOLD and not self.reconnecting:
                    if now_ts - self.last_reconnect_time > self.reconnect_cooldown:
                        await self._auto_reconnect()
            return

        # Frozen-feed detection (skip during blackout — CME halt causes expected freeze)
        if any_price_frozen and in_session() and not in_blackout() and not self.reconnecting:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [FROZEN] {frozen_sym} price unchanged 180+s — reconnecting")
            self._notify_status(f"STATUS|{frozen_sym} feed frozen, reconnecting ({now} ET)")
            if now_ts - self.last_reconnect_time > self.reconnect_cooldown:
                await self._auto_reconnect()
                return

        self.last_price_time = now_ts
        if not self.connection_alive:
            self.connection_alive = True
            self.disconnect_alert_sent = False
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [ALERT] Price data restored")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|RECONNECTED ({now} ET)")

        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        if sess_ended:
            for sym, st in self.states.items():
                if st.position != 0:
                    print(f"[SESSION] {sym} session ended — flattening")
                    p = await st.ctx.data.get_current_price()
                    if p:
                        await st._flatten(p, reason="SESSION_END")
                        send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                     "FLAT", sym, p, 0, ntfy_topic=st.ntfy_topic,
                                     tp_webhooks=st.tp_webhooks)
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] Disconnecting until next session")
            if self.suite:
                try:
                    await self.suite.disconnect()
                except Exception:
                    pass
                self.suite = None
                for st in self.states.values():
                    st.ctx = None
            self.was_in_session = currently_in_session
            return

        sess_started = not self.was_in_session and currently_in_session
        if sess_started:
            for st in self.states.values():
                st.live_pnl = 0.0
            if self.suite is None:
                from project_x_py import TradingSuite
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                symbols = self._symbols_list()
                print(f"[{now_str}] [SESSION] Reconnecting for new session...")
                self.suite = await TradingSuite.create(
                    instruments=symbols,
                    timeframes=["1sec", "15min"],
                    initial_days=1,
                )
                self._register_websocket_handlers()
                for sym, st in self.states.items():
                    st.ctx = self.suite[sym]
                    await st.seed_history()
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] New session started — LIVE")
            for st in self.states.values():
                st.print_status()

        self.was_in_session = currently_in_session
        if not currently_in_session:
            return

        # Skip trading during CME daily halt but keep connection alive
        if in_blackout():
            return

        # Run each symbol's strategy + position reconciliation
        connection_error = False
        for sym, st in self.states.items():
            try:
                await st.tick(cached_price=cached_prices.get(sym))
                if st.position != 0:
                    await st.reconcile_position_with_platform()
            except Exception as e:
                print(f"[WARN] {st.symbol} tick raised: {e}")
                connection_error = True
            if st.last_order_error in ("timeout", "error"):
                connection_error = True
                st.last_order_error = None

        if connection_error and not self.reconnecting:
            if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
                await self._auto_reconnect()

        # Stale-data detection: only reconnect if price feed is truly dead
        # (no new brick AND price frozen). Low-volatility periods can go 10+ min
        # without a new brick even though the feed is alive.
        if not self.reconnecting:
            for sym, st in self.states.items():
                if st.is_data_stale(threshold=600) and st.is_price_frozen(threshold=180):
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [STALE] {sym} no new brick 600s AND price frozen 180s — reconnecting")
                    self._notify_status(f"STATUS|{sym} data stale, reconnecting ({now} ET)")
                    if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
                        await self._auto_reconnect()
                    break

        # Periodic state save
        if time.time() - self.last_state_save > 30:
            self.save_all_state()
            self.last_state_save = time.time()

        # GatewayLogout: SDK fires this frequently but feed keeps working.
        # Reconnecting on it kills a working connection and causes position losses.
        if self.last_gateway_logout_time > 0 and time.time() - self.last_gateway_logout_time > 5:
            self.last_gateway_logout_time = 0

        # Periodic GC + memory logging (every 5 min)
        if time.time() - self.last_gc_time > self.GC_INTERVAL:
            self.last_gc_time = time.time()
            cutoff = time.time() - 60
            self._ws_close_seen = {k: v for k, v in self._ws_close_seen.items() if v > cutoff}
            gc.collect()
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            rss_mb = rss_kb // 1024
                            now = datetime.now(ET).strftime("%H:%M:%S")
                            print(f"[{now}] [MEM] RSS: {rss_mb}MB (gc collected)")
                            if rss_mb > 500:
                                print(f"[{now}] [MEM] RSS {rss_mb}MB exceeds 500MB limit - saving state and restarting...")
                                self.save_all_state()
                                os._exit(0)
                            break
            except Exception:
                pass

        # Heartbeat (every 30 min)
        if time.time() - self.last_heartbeat > self.HEARTBEAT_INTERVAL:
            self.last_heartbeat = time.time()
            now = datetime.now(ET).strftime("%H:%M:%S")
            for sym, st in self.states.items():
                pos_str = ("FLAT" if st.position == 0
                           else "LONG" if st.position == 1 else "SHORT")
                mfi_str = f"{st.mfi_value:.2f}" if st.mfi_value is not None else "N/A"
                rsg_str = f"{st.renko_sma:.2f}" if st.renko_sma is not None else "N/A"
                msg = (f"HEARTBEAT|{sym} alive ({now} ET) | {pos_str} | "
                       f"P&L: ${st.live_pnl:.2f} | MFI: {mfi_str} | R.sg: {rsg_str}")
                threading.Thread(target=send_telegram, args=(
                    self.tg_token, self.tg_chat, msg), daemon=True).start()

    async def _shutdown(self):
        self.save_all_state()
        print("\n[BOT] Shutdown (state saved, positions kept open)...")
        for sym, st in self.states.items():
            if st.position != 0:
                dir_str = "LONG" if st.position == 1 else "SHORT"
                print(f"  [{sym}] Keeping {dir_str} x{st.contracts_held} open (entry: {st.entry_price:.2f})")

        print(f"\n[BOT] === SESSION SUMMARY ===")
        for sym, st in self.states.items():
            print(f"[BOT] {sym} P&L: ${st.live_pnl:.2f}")
        total = sum(st.live_pnl for st in self.states.values())
        print(f"[BOT] Total P&L: ${total:.2f}")
        print(f"[BOT] ========================")

        if self.suite:
            try:
                await asyncio.wait_for(self.suite.disconnect(), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def parse_symbol_configs(symbols_str: str) -> list:
    """Parse 'NQ:5:1:ntfy-topic,ES:2:1' into list of config dicts."""
    configs = []
    for part in symbols_str.split(","):
        parts = part.strip().split(":")
        if len(parts) < 3:
            raise ValueError(f"Invalid symbol config '{part}'. "
                             f"Format: SYMBOL:BRICK_SIZE:QTY[:NTFY_TOPIC]")
        cfg = {
            "symbol": parts[0].strip().upper(),
            "brick_size": float(parts[1]),
            "qty": int(parts[2]),
            "ntfy_topic": parts[3].strip() if len(parts) > 3 else "",
        }
        configs.append(cfg)
    return configs


def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko MFI Bot (Multi-Symbol)")
    parser.add_argument("--symbol", default="", help="Single symbol (backward compat)")
    parser.add_argument("--symbols", default="",
                        help="Multi-symbol config: 'NQ:5:1:ntfy,ES:2:1'")
    parser.add_argument("--qty", type=int, default=1, help="Qty for single --symbol mode")
    parser.add_argument("--brick-size", type=float, default=5.0,
                        help="Brick size for single --symbol mode")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--ntfy-topic", default="", help="ntfy.sh topic (single --symbol mode)")
    parser.add_argument("--tick-interval", type=int, default=1,
                        help="Reserved for future per-symbol gating (current: 0.5s loop)")
    parser.add_argument("--use-rsg-filter", action="store_true",
                        help="Skip MFI signals when brick close is within 1 brick_size of R.sg line")
    parser.add_argument("--tp-webhooks", default="",
                        help="Comma-separated TradersPost webhook URLs")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []
    tp_webhooks = [u.strip() for u in args.tp_webhooks.split(",") if u.strip()] if args.tp_webhooks else []

    if args.symbols:
        symbol_configs = parse_symbol_configs(args.symbols)
    elif args.symbol:
        symbol_configs = [{
            "symbol": args.symbol.upper(),
            "brick_size": args.brick_size,
            "qty": args.qty,
            "ntfy_topic": args.ntfy_topic,
        }]
    else:
        symbol_configs = [{"symbol": "NQ", "brick_size": 5.0, "qty": 1, "ntfy_topic": ""}]

    stopped = False
    retry_delay = 30
    last_crash_notify = 0
    CRASH_NOTIFY_COOLDOWN = 300
    current_bot = None

    def handle_signal(sig, frame):
        nonlocal stopped
        stopped = True
        if current_bot:
            current_bot.running = False
            current_bot.save_all_state()
        print("\n[BOT] Shutting down...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Truncate log if too large
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 10_000_000:
            os.truncate(log_file, 0)
            print(f"[BOT] Log file truncated (>10MB)")
    except Exception:
        pass

    while not stopped:
        bot = RenkoBot(
            symbol_configs=symbol_configs,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            tick_interval=args.tick_interval,
            use_rsg_filter=args.use_rsg_filter,
            tp_webhooks=tp_webhooks,
        )
        current_bot = bot

        loop = asyncio.new_event_loop()
        run_start = time.time()

        try:
            loop.run_until_complete(bot.run())
            retry_delay = 30
        except KeyboardInterrupt:
            if current_bot:
                current_bot.save_all_state()
            break
        except BaseException as e:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"\n[{now}] [CRASH] {type(e).__name__}: {e}")
            print(f"[{now}] [CRASH] Restarting in {retry_delay}s...")
            if time.time() - last_crash_notify > CRASH_NOTIFY_COOLDOWN:
                send_telegram(args.tg_token, args.tg_chat,
                              f"STATUS|Bot crashed, restarting in {retry_delay}s ({now} ET)")
                last_crash_notify = time.time()
            run_duration = time.time() - run_start
            if run_duration > 300:
                retry_delay = 30
            else:
                retry_delay = min(retry_delay * 2, 300)
        finally:
            if current_bot:
                current_bot.save_all_state()
            try:
                loop.close()
            except Exception:
                pass

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
