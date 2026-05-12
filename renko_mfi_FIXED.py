"""
TopstepX Renko MFI Bot (LIVE)
=============================
Multi-symbol support: runs multiple instruments on one connection.

Strategy: MFI Oversold/Overbought dot + Same-color brick confirmation
- MFI(14) on Renko bricks (volume = 1 per brick; values will not match TradingView
  exactly because TopstepX does not expose real volume — this is intentional and
  documented)
- Oversold dot: MFI crosses BELOW 20 (prev > 20, new <= 20)
- Overbought dot: MFI crosses ABOVE 85 (prev < 85, new >= 85)
- LONG  entry: oversold dot armed AND a GREEN brick confirms in same cycle
- SHORT entry: overbought dot armed AND a RED   brick confirms in same cycle
- EXIT: opposite MFI signal flips the position. No bot-side stop/take-profit;
  customer manages exits via TopstepX platform stop/take ($50 default).

Hard requirements implemented:
- Position safety: WS event handler + periodic HTTP poll with divergence
  counter to avoid SDK get_all_positions() false-flat bug.
- Indicator persistence across long downtimes (no 600s TTL on indicator state).
- Frozen-feed detection (websocket alive but price stuck).
- Exponential reconnect backoff (5s -> 120s).
- Multi-symbol parallel health check.
- Order placement / position close protected by asyncio.shield so a
  CancelledError mid-call cannot orphan a position.
- Single position per symbol — no scaling.

Usage:
    python renko_mfi_bot.py --symbols "NQ:5:1:ntfy-topic" --tick-interval 1
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
SESSION_START = dtime(19, 0, 0)   # 7:00 PM ET start
SESSION_END = dtime(15, 30, 0)    # 3:30 PM ET end
TRADING_DAYS = [0, 1, 2, 3, 4, 6]  # Mon-Fri (0-4) + Sun (6). Saturday excluded.
# CME daily settlement halt: 4:15-4:30 PM ET (with buffer)
BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

# Indicator parameters
MFI_PERIOD = 14
MFI_OVERSOLD = 20.0
MFI_OVERBOUGHT = 85.0

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
# ML Trade Filter (KNN-based pattern matching)
# ============================================================

ML_WARMUP_TRADES = 30
ML_K_NEIGHBORS = 7
ML_SKIP_THRESHOLD = 0.65


class TradeML:
    # Feature set tuned for MFI strategy
    FEATURE_NAMES = [
        "direction",         # 1=long, -1=short
        "mfi_value",         # MFI at entry
        "mfi_velocity",      # MFI(now) - MFI(prev)
        "rsg_distance",      # (price - R.sg_line) / brick_size
        "ema_distance",      # (price - EMA) / brick_size (general trend feature)
        "hour",              # ET hour 0-23
        "volatility",        # std of last 10 brick closes / brick_size
    ]

    def __init__(self, data_file: str):
        self.data_file = data_file
        self.trades = []
        self._load()

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r") as f:
                    self.trades = json.load(f)
                print(f"[ML] Loaded {len(self.trades)} historical trades")
            except Exception as e:
                print(f"[ML] Load error: {e}")
                self.trades = []

    def _save(self):
        try:
            with open(self.data_file, "w") as f:
                json.dump(self.trades, f, indent=1)
        except Exception as e:
            print(f"[ML] Save error: {e}")

    def record_trade(self, features: dict, pnl: float, source: str = "live"):
        if features is None:
            return
        trade = {
            "features": features,
            "pnl": pnl,
            "win": 1 if pnl > 0 else 0,
            "source": source,
            "timestamp": datetime.now(ET).isoformat(),
        }
        self.trades.append(trade)
        self._save()
        wins = sum(1 for t in self.trades if t["win"] == 1)
        total = len(self.trades)
        print(f"[ML] Trade recorded: PnL=${pnl:.2f} | Total: {total} trades, "
              f"{wins} wins ({100 * wins / total:.0f}%)")

    def extract_features(self, direction: int, mfi_value, mfi_velocity,
                         price: float, ema, rsg, brick_size: float,
                         brick_closes: list) -> dict:
        # Defensive: any of these may be None if called before warmup.
        mfi_v = float(mfi_value) if mfi_value is not None else 50.0
        mfi_vel = float(mfi_velocity) if mfi_velocity is not None else 0.0
        ema_distance = ((price - ema) / brick_size) if (ema is not None and brick_size > 0) else 0.0
        rsg_distance = ((price - rsg) / brick_size) if (rsg is not None and brick_size > 0) else 0.0
        hour = datetime.now(ET).hour

        vol = 0.0
        if len(brick_closes) >= 10:
            vol = float(np.std(brick_closes[-10:])) / brick_size if brick_size > 0 else 0.0

        return {
            "direction": direction,
            "mfi_value": round(mfi_v, 2),
            "mfi_velocity": round(mfi_vel, 4),
            "rsg_distance": round(rsg_distance, 4),
            "ema_distance": round(ema_distance, 4),
            "hour": hour,
            "volatility": round(vol, 4),
        }

    def should_skip(self, features: dict) -> tuple:
        total = len(self.trades)
        if total < ML_WARMUP_TRADES:
            return False, 0.0, f"warmup ({total}/{ML_WARMUP_TRADES})"

        feat_vec = self._to_vector(features)
        all_vecs = np.array([self._to_vector(t["features"]) for t in self.trades])
        all_wins = np.array([t["win"] for t in self.trades])

        means = all_vecs.mean(axis=0)
        stds = all_vecs.std(axis=0)
        stds[stds < 1e-8] = 1.0

        norm_feat = (feat_vec - means) / stds
        norm_all = (all_vecs - means) / stds

        distances = np.sqrt(np.sum((norm_all - norm_feat) ** 2, axis=1))
        k = min(ML_K_NEIGHBORS, total)
        nearest_idx = np.argpartition(distances, k - 1)[:k]
        nearest_wins = all_wins[nearest_idx]

        loss_ratio = 1.0 - nearest_wins.mean()
        skip = loss_ratio >= ML_SKIP_THRESHOLD
        reason = f"KNN({k}): {int(nearest_wins.sum())}/{k} wins, loss_ratio={loss_ratio:.2f}"
        return skip, loss_ratio, reason

    def _to_vector(self, features: dict) -> np.ndarray:
        return np.array([features.get(name, 0.0) for name in self.FEATURE_NAMES], dtype=float)

    def stats(self) -> str:
        if not self.trades:
            return "No trades recorded"
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"] == 1)
        losses = total - wins
        total_pnl = sum(t["pnl"] for t in self.trades)
        return (f"Total: {total} | Wins: {wins} | Losses: {losses} | "
                f"Win%: {100 * wins / total:.0f}% | PnL: ${total_pnl:.2f}")


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

        # MFI state
        self.mfi_value = None
        self.prev_mfi_value = None
        self.mfi_oversold_dot = False
        self.mfi_overbought_dot = False

        # Break-of-structure confirmation (BOS)
        # When MFI signal fires, we don't enter immediately. Instead we arm a
        # pending entry and wait for 2 confirmation Renko bricks (2-pt, 5s feed)
        # to close in the signal direction beyond the break level.
        self.bos_armed = None          # "LONG" or "SHORT" or None
        self.bos_break_level = None    # brick high (buy) or brick low (sell)
        self.bos_confirm_renko = RenkoEngine(2.0, f"{symbol}-BOS")
        self.bos_confirm_count = 0     # consecutive bricks in signal direction past level
        self.bos_confirm_needed = 2
        self._last_bos_feed_time = 0.0
        self.BOS_FEED_INTERVAL = 5.0   # feed confirmation renko every 5 seconds

        # 15-second MACD filter (standard 12/26/9 on normal candles)
        # After BOS confirms, MACD histogram must agree before entry.
        self.macd_candle_interval = 15.0  # seconds
        self._candle_start_time = 0.0
        self._candle_open = None
        self._candle_high = None
        self._candle_low = None
        self._candle_close = None
        self.macd_closes = []       # 15-sec candle close history
        self.macd_fast_ema = None   # EMA-12
        self.macd_slow_ema = None   # EMA-26
        self.macd_signal_ema = None # Signal EMA-9
        self.macd_hist = None       # current histogram
        self.macd_prev_hist = None  # previous histogram
        self.bos_confirmed_waiting_macd = False  # BOS confirmed but waiting for MACD

        # Position state (scale-in up to MAX_CONTRACTS)
        self.position = 0           # 1 long, -1 short, 0 flat
        self.contracts_held = 0
        self.entry_price = 0.0      # weighted average entry
        self.entry_time = None
        self.entry_features = None  # snapshot for ML
        self.live_pnl = 0.0
        self.MAX_CONTRACTS = 5
        self.TP_BASE_DOLLARS = 100.0
        self.TP_INCREMENT_DOLLARS = 50.0

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
            "bos_armed": self.bos_armed,
            "bos_break_level": self.bos_break_level,
            "bos_confirm_count": self.bos_confirm_count,
            "bos_confirmed_waiting_macd": self.bos_confirmed_waiting_macd,
            # MACD 15-sec candle state
            "macd_closes": self.macd_closes[-100:],
            "macd_fast_ema": self.macd_fast_ema,
            "macd_slow_ema": self.macd_slow_ema,
            "macd_signal_ema": self.macd_signal_ema,
            "macd_hist": self.macd_hist,
            "macd_prev_hist": self.macd_prev_hist,
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
        # Restore BOS state (armed signal survives restarts)
        self.bos_armed = state.get("bos_armed")
        self.bos_break_level = state.get("bos_break_level")
        self.bos_confirm_count = state.get("bos_confirm_count", 0)
        self.bos_confirmed_waiting_macd = state.get("bos_confirmed_waiting_macd", False)
        if self.bos_armed:
            self.bos_confirm_renko = RenkoEngine(2.0, f"{self.symbol}-BOS")
            if self.renko.last_close is not None:
                self.bos_confirm_renko.initialize(self.renko.last_close)
        # Restore MACD 15-sec candle state
        self.macd_closes = state.get("macd_closes", [])
        self.macd_fast_ema = state.get("macd_fast_ema")
        self.macd_slow_ema = state.get("macd_slow_ema")
        self.macd_signal_ema = state.get("macd_signal_ema")
        self.macd_hist = state.get("macd_hist")
        self.macd_prev_hist = state.get("macd_prev_hist")
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
        self.brick_volumes.append(1)
        h = max(brick_open, brick_close)
        l = min(brick_open, brick_close)
        self.brick_typicals.append((h + l + brick_close) / 3.0)
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

    def _update_macd_candle(self, price: float, now_ts: float):
        """Build 15-second candles and compute MACD(12,26,9) histogram."""
        if self._candle_open is None:
            self._candle_start_time = now_ts
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_close = price
            return

        self._candle_high = max(self._candle_high, price)
        self._candle_low = min(self._candle_low, price)
        self._candle_close = price

        if now_ts - self._candle_start_time >= self.macd_candle_interval:
            self.macd_closes.append(self._candle_close)
            if len(self.macd_closes) > 200:
                self.macd_closes = self.macd_closes[-200:]

            c = self._candle_close
            # EMA-12 (fast)
            if self.macd_fast_ema is None:
                if len(self.macd_closes) >= 12:
                    self.macd_fast_ema = sum(self.macd_closes[-12:]) / 12
            else:
                k = 2.0 / 13.0
                self.macd_fast_ema = c * k + self.macd_fast_ema * (1 - k)

            # EMA-26 (slow)
            if self.macd_slow_ema is None:
                if len(self.macd_closes) >= 26:
                    self.macd_slow_ema = sum(self.macd_closes[-26:]) / 26
            else:
                k = 2.0 / 27.0
                self.macd_slow_ema = c * k + self.macd_slow_ema * (1 - k)

            # MACD line and signal
            if self.macd_fast_ema is not None and self.macd_slow_ema is not None:
                macd_line = self.macd_fast_ema - self.macd_slow_ema
                if self.macd_signal_ema is None:
                    self.macd_signal_ema = macd_line
                else:
                    k = 2.0 / 10.0
                    self.macd_signal_ema = macd_line * k + self.macd_signal_ema * (1 - k)

                self.macd_prev_hist = self.macd_hist
                self.macd_hist = macd_line - self.macd_signal_ema

            # Start new candle
            self._candle_start_time = now_ts
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_close = price

    def macd_agrees(self, direction: str) -> bool:
        """Check if MACD histogram shift agrees with entry direction.
        For BUY: histogram must be shifting up (current > previous).
        For SELL: histogram must be shifting down (current < previous)."""
        if self.macd_hist is None or self.macd_prev_hist is None:
            return True  # not enough data yet, allow entry
        if direction == "LONG":
            return self.macd_hist > self.macd_prev_hist
        else:
            return self.macd_hist < self.macd_prev_hist

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

        for row in rows:
            close = float(row["close"])
            for brick in self.renko.feed_close(close):
                self._add_brick(brick[0], brick[1])
                self._calc_indicators()

        # Always clear dots after seed: never act on a "dot armed" state
        # synthesized from historical data.
        self.mfi_oversold_dot = False
        self.mfi_overbought_dot = False

        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"
        rma_str = f"{self.renko_sma:.2f}" if self.renko_sma is not None else "N/A"
        mfi_str = f"{self.mfi_value:.2f}" if self.mfi_value is not None else "N/A"
        last_close_str = f"{self.renko.last_close:.2f}" if self.renko.last_close is not None else "N/A"
        print(f"  [{self.symbol}] Renko: {self.renko.brick_count} bricks, {dir_str}, ref={last_close_str}")
        print(f"  [{self.symbol}] R.sg SMA({RENKO_SMA_PERIOD}): {rma_str} | MFI({MFI_PERIOD}): {mfi_str}")

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
        bos_str = f"ARMED {self.bos_armed} @ {self.bos_break_level:.2f} ({self.bos_confirm_count}/{self.bos_confirm_needed})" if self.bos_armed else "none"
        print(f"    Oversold dot: {os_str} | Overbought dot: {ob_str}")
        macd_h = f"{self.macd_hist:.4f}" if self.macd_hist is not None else "N/A"
        macd_wait = " [WAIT-MACD]" if self.bos_confirmed_waiting_macd else ""
        print(f"    BOS: {bos_str}{macd_wait} | MACD hist: {macd_h}")
        tp_str = ""
        if self.contracts_held > 0:
            tp = self.TP_BASE_DOLLARS + self.TP_INCREMENT_DOLLARS * (self.contracts_held - 1)
            tp_str = f" | TP: ${tp:.0f}"
        print(f"    Position: {pos_str} x{self.contracts_held} | P&L: ${self.live_pnl:.2f} | "
              f"PV=${self.point_value}/pt{tp_str}")

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
                    size = float(getattr(p, "size", 0) or getattr(p, "net_pos", 0) or 0)
                    side = getattr(p, "side", 0)
                    return int(size) if side == 0 else -int(size)
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
                    self.ml.record_trade(self.entry_features, pnl_est, source="platform_close")
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
        self.last_price = price

        now_ts = time.time()
        now = datetime.now(ET).strftime("%H:%M:%S")

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

                # SIGNAL: oversold dot armed AND green brick = ARM BOS
                if self.mfi_oversold_dot and brick_dir == 1:
                    self.mfi_oversold_dot = False  # consume
                    break_level = b[1]  # brick close (high of bullish brick)
                    self.bos_armed = "LONG"
                    self.bos_break_level = break_level
                    self.bos_confirm_count = 0
                    self.bos_confirm_renko = RenkoEngine(2.0, f"{self.symbol}-BOS")
                    self.bos_confirm_renko.initialize(price)
                    print(f"[{now}] [{self.symbol} BOS-ARM] LONG armed @ {break_level:.2f} "
                          f"| waiting for 2x 2pt bricks above level")
                    break

                # SIGNAL: overbought dot armed AND red brick = ARM BOS
                elif self.mfi_overbought_dot and brick_dir == -1:
                    self.mfi_overbought_dot = False  # consume
                    break_level = b[1]  # brick close (low of bearish brick)
                    self.bos_armed = "SHORT"
                    self.bos_break_level = break_level
                    self.bos_confirm_count = 0
                    self.bos_confirm_renko = RenkoEngine(2.0, f"{self.symbol}-BOS")
                    self.bos_confirm_renko.initialize(price)
                    print(f"[{now}] [{self.symbol} BOS-ARM] SHORT armed @ {break_level:.2f} "
                          f"| waiting for 2x 2pt bricks below level")
                    break

        # Update 15-second MACD candles (every tick)
        self._update_macd_candle(price, now_ts)

        # Break-of-structure confirmation check (2-pt Renko, 5s feed interval)
        if self.bos_armed is not None and not self.bos_confirmed_waiting_macd:
            if now_ts - self._last_bos_feed_time >= self.BOS_FEED_INTERVAL:
                self._last_bos_feed_time = now_ts
                bos_bricks = self.bos_confirm_renko.feed_close(price)
                for bb in bos_bricks:
                    bb_dir = bb[2]  # 1 bullish, -1 bearish
                    bb_close = bb[1]
                    if self.bos_armed == "LONG" and bb_dir == 1 and bb_close > self.bos_break_level:
                        self.bos_confirm_count += 1
                        print(f"[{now}] [{self.symbol} BOS] LONG confirm brick {self.bos_confirm_count}/{self.bos_confirm_needed} "
                              f"@ {bb_close:.2f} (level: {self.bos_break_level:.2f})")
                    elif self.bos_armed == "SHORT" and bb_dir == -1 and bb_close < self.bos_break_level:
                        self.bos_confirm_count += 1
                        print(f"[{now}] [{self.symbol} BOS] SHORT confirm brick {self.bos_confirm_count}/{self.bos_confirm_needed} "
                              f"@ {bb_close:.2f} (level: {self.bos_break_level:.2f})")
                    else:
                        self.bos_confirm_count = 0

                    if self.bos_confirm_count >= self.bos_confirm_needed:
                        if self.macd_agrees(self.bos_armed):
                            direction = self.bos_armed
                            hist_str = f"{self.macd_hist:.4f}" if self.macd_hist is not None else "N/A"
                            print(f"[{now}] [{self.symbol} BOS+MACD] {direction} confirmed! "
                                  f"MACD hist={hist_str} agrees | Entering @ {price:.2f}")
                            self.bos_armed = None
                            self.bos_break_level = None
                            self.bos_confirm_count = 0
                            self.bos_confirmed_waiting_macd = False
                            await self._handle_signal(direction, price, now)
                        else:
                            hist_str = f"{self.macd_hist:.4f}" if self.macd_hist is not None else "N/A"
                            prev_str = f"{self.macd_prev_hist:.4f}" if self.macd_prev_hist is not None else "N/A"
                            print(f"[{now}] [{self.symbol} BOS-WAIT-MACD] {self.bos_armed} BOS confirmed but "
                                  f"MACD disagrees (hist={hist_str}, prev={prev_str}). Waiting for shift...")
                            self.bos_confirmed_waiting_macd = True
                        break

        # BOS confirmed but waiting for MACD histogram shift
        if self.bos_confirmed_waiting_macd and self.bos_armed is not None:
            if self.macd_agrees(self.bos_armed):
                direction = self.bos_armed
                hist_str = f"{self.macd_hist:.4f}" if self.macd_hist is not None else "N/A"
                print(f"[{now}] [{self.symbol} MACD-SHIFT] {direction} MACD now agrees! "
                      f"hist={hist_str} | Entering @ {price:.2f}")
                self.bos_armed = None
                self.bos_break_level = None
                self.bos_confirm_count = 0
                self.bos_confirmed_waiting_macd = False
                await self._handle_signal(direction, price, now)

        # Take profit check (incrementing: $100 base + $50 per additional contract)
        if self.position != 0 and self.contracts_held > 0:
            contracts = self.contracts_held
            tp_target = self.TP_BASE_DOLLARS + self.TP_INCREMENT_DOLLARS * (contracts - 1)
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.point_value * contracts
            else:
                unrealized = (self.entry_price - price) * self.point_value * contracts
            if unrealized >= tp_target:
                direction = "LONG" if self.position == 1 else "SHORT"
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] [{self.symbol} TP] ${tp_target:.0f} target hit! "
                      f"{direction} x{contracts} | Unrealized: ${unrealized:.2f}")
                await self._flatten(price, reason="TP_HIT")
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "FLAT", self.symbol, price, 0),
                    kwargs={"ntfy_topic": self.ntfy_topic,
                            "tp_webhooks": self.tp_webhooks}, daemon=True).start()

        return True

    async def _handle_signal(self, direction: str, price: float, now: str):
        """Fire entry/flip logic for a confirmed MFI+brick signal."""
        # R.sg distance filter (optional)
        if self.use_rsg_filter and self.renko_sma is not None:
            last_bc = self.brick_closes[-1] if self.brick_closes else price
            if abs(last_bc - self.renko_sma) <= self.brick_size:
                print(f"[{now}] [{self.symbol} RSG-FILTER] {direction} skipped: brick_close "
                      f"{last_bc:.2f} too close to R.sg {self.renko_sma:.2f} "
                      f"(<= {self.brick_size:.2f} pts)")
                return

        # If already in position, handle scale-in or flip
        if self.position != 0:
            already_dir = "LONG" if self.position == 1 else "SHORT"
            if (direction == "LONG" and self.position == 1) or (direction == "SHORT" and self.position == -1):
                if self.contracts_held >= self.MAX_CONTRACTS:
                    print(f"[{now}] [{self.symbol}] {direction} signal: already {already_dir} x{self.contracts_held} (max)")
                    return
                old_qty = self.contracts_held
                print(f"[{now}] [{self.symbol} SCALE-IN] {direction} #{old_qty + 1}")
                side = 0 if direction == "LONG" else 1
                success = await self._enter_addon(price, side=side)
                if success:
                    new_avg = (self.entry_price * old_qty + price) / (old_qty + 1)
                    self.entry_price = new_avg
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        direction, self.symbol, price, self.contracts_held),
                        kwargs={"ntfy_topic": self.ntfy_topic,
                                "tp_webhooks": self.tp_webhooks}, daemon=True).start()
                return

            # Opposite-side flip
            print(f"[{now}] [{self.symbol} FLIP] {already_dir} x{self.contracts_held} -> {direction}: closing first")
            trade_pnl = ((price - self.entry_price) * self.position
                         * self.point_value * self.contracts_held)
            if self.ml and self.entry_features:
                self.ml.record_trade(self.entry_features, trade_pnl, source="signal_flip")
                self.entry_features = None
            await self._flatten(price, reason="SIGNAL_FLIP")
            if self.position != 0:
                print(f"[{now}] [{self.symbol}] flatten failed, aborting flip")
                return

        # Build ML features and ask filter
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
        ) if self.ml else None

        if features and self.ml:
            skip, _, reason = self.ml.should_skip(features)
            if skip:
                print(f"[{now}] [{self.symbol} ML-SKIP] {direction} skipped | {reason}")
                send_telegram(self.tg_token, self.tg_chat,
                              f"ML-SKIP|{self.symbol} {direction} @ {price:.2f} | {reason}")
                return
            print(f"[{now}] [{self.symbol} ML-OK] {direction} approved | {reason}")
        self.entry_features = features

        # Place the entry
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
                    self.contracts_held = 1
                    self.entry_price = price
                    self.entry_time = datetime.now(ET)
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
                    self.contracts_held = 1
                    self.entry_price = price
                    self.entry_time = datetime.now(ET)
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

    async def _enter_addon(self, price: float, side: int):
        """Scale-in: add 1 contract to existing position."""
        async def _do_order():
            try:
                response = await asyncio.wait_for(
                    self.ctx.orders.place_market_order(
                        contract_id=self.ctx.instrument_info.id,
                        side=side, size=self.qty),
                    timeout=15.0)
                if response.success:
                    self.contracts_held += 1
                    print(f"[{self.symbol}] Scale-in filled #{self.contracts_held}. ID: {response.orderId}")
                    return True
                print(f"[{self.symbol}] Scale-in REJECTED: {response.errorMessage}")
                return False
            except asyncio.TimeoutError:
                print(f"[{self.symbol}] Scale-in TIMEOUT")
                return False
            except Exception as ex:
                print(f"[{self.symbol}] Scale-in ERROR: {ex}")
                return False

        return await asyncio.shield(_do_order())

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
            self.live_pnl += trade_pnl
            self.position = 0
            self.contracts_held = 0
            self.entry_price = 0.0
            self.entry_time = None
            self._log_trade(direction, saved_entry_price, price, trade_pnl, reason, saved_entry_time)
        return close_ok

    def _log_trade(self, direction, entry_price, exit_price, pnl, reason, entry_time=None):
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

        self.ml = None

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
        self.STALE_THRESHOLD = 180
        self.RECONNECT_THRESHOLD = 300

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
        print(f"[BOT] Renko MFI Strategy - LIVE MODE")
        print(f"[BOT] Symbols: {', '.join(symbols)}")
        for sym, st in self.states.items():
            print(f"[BOT]   {sym}: brick={st.brick_size}, qty={st.qty}, "
                  f"pv=${st.point_value}/pt"
                  + (f", ntfy={st.ntfy_topic}" if st.ntfy_topic else ""))
        print(f"[BOT] ENTRY: MFI dot ({MFI_OVERSOLD}/{MFI_OVERBOUGHT}) + same-color brick confirmation")
        print(f"[BOT] EXIT: opposite MFI signal flips position (no bot-side TP/SL — platform manages)")
        rsg_state = "ENABLED" if any(s.use_rsg_filter for s in self.states.values()) else "disabled"
        print(f"[BOT] R.sg distance filter: {rsg_state}")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - "
              f"{SESSION_END.strftime('%H:%M')} ET ({trading_day_str})")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        if self.ml:
            print(f"[BOT] ML filter: {self.ml.stats()}")
            print(f"[BOT] ML settings: warmup={ML_WARMUP_TRADES}, K={ML_K_NEIGHBORS}, "
                  f"skip_threshold={ML_SKIP_THRESHOLD}")
        else:
            print("[BOT] ML filter: DISABLED")
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
        print(f"[BOT] Trading LIVE - MFI strategy ({', '.join(symbols)})")
        print(f"[BOT] Press Ctrl+C to stop\n")

        try:
            while self.running:
                try:
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
                                               source="ws_platform_close")
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

            # Safety flatten: any open position during reconnect gets closed,
            # because we may have missed exit events.
            for sym, st in self.states.items():
                if st.position != 0:
                    direction = "LONG" if st.position == 1 else "SHORT"
                    print(f"[{now}] [SAFETY] {sym} was {direction} during disconnect — flattening")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"STATUS|SAFETY FLATTEN {sym} ({now} ET)")
                    try:
                        price = await st.ctx.data.get_current_price()
                        if price:
                            await st._flatten(price, reason="SAFETY_RECONNECT")
                            send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                         "FLAT", sym, price, 0, ntfy_topic=st.ntfy_topic,
                                         tp_webhooks=st.tp_webhooks)
                    except Exception as e:
                        print(f"[{now}] [SAFETY] {sym} flatten failed: {e}")

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
                p = await st.ctx.data.get_current_price()
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

        # Stale-data detection: no new brick in 10+ minutes during session
        if not self.reconnecting:
            for sym, st in self.states.items():
                if st.is_data_stale(threshold=600):
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [STALE] {sym} no new brick 600s — reconnecting")
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
        print("\n[BOT] Shutdown (state saved)...")
        for sym, st in self.states.items():
            if st.position != 0 and st.ctx:
                try:
                    price = await asyncio.wait_for(
                        st.ctx.data.get_current_price(), timeout=3.0)
                    if price:
                        await asyncio.wait_for(
                            st._flatten(price, reason="SHUTDOWN"), timeout=5.0)
                        send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                     "FLAT", sym, price, 0, ntfy_topic=st.ntfy_topic,
                                     tp_webhooks=st.tp_webhooks)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:
                    print(f"  [{sym}] Shutdown flatten failed: {e}")

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
