"""
TopstepX Renko EMA + MACD Strategy Bot (LIVE)
Multi-symbol support: runs multiple instruments on one connection.

Strategy: Renko Close + EMA 9 Cross + MACD Confirmation
- LONG when brick close crosses above EMA 9 AND MACD histogram > 0
- SHORT when brick close crosses below EMA 9 AND MACD histogram < 0
- EXIT on EMA 9 flip (regardless of MACD)

Usage:
    python renko_bot.py --symbols "NQ:3:1:ntfy-topic" --tick-interval 60
"""

import asyncio
import argparse
import signal
import json
import os
import time
import threading
import math
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


def send_signals(token: str, chat_id: str, keys: list, direction: str, symbol: str, price: float, qty: int, ntfy_topic: str = ""):
    for i, key in enumerate(keys):
        if i > 0:
            time.sleep(0.5)
        msg = f"SIGNAL|{key}|{direction}|{symbol}|{price}|{qty}"
        send_telegram(token, chat_id, msg)
        send_ntfy(ntfy_topic, msg)


# ============================================================
# Renko Engine (Traditional - TradingView exact)
# ============================================================

class RenkoEngine:
    def __init__(self, brick_size: float, label: str = ""):
        self.brick_size = brick_size
        self.label = label
        self.last_close = None
        self.direction = 0
        self.brick_count = 0

    def initialize(self, price: float):
        self.last_close = round(price / self.brick_size) * self.brick_size

    def feed_close(self, close_price: float) -> list:
        if self.last_close is None:
            self.initialize(close_price)
            return []

        new_bricks = []
        while True:
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
        return new_bricks

    def feed_ohlc(self, high: float, low: float, close: float) -> list:
        if self.last_close is None:
            self.initialize(close)
            return []

        new_bricks = []
        if self.direction >= 0:
            new_bricks.extend(self.feed_close(high))
            new_bricks.extend(self.feed_close(low))
        else:
            new_bricks.extend(self.feed_close(low))
            new_bricks.extend(self.feed_close(high))
        new_bricks.extend(self.feed_close(close))
        return new_bricks


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(18, 0, 0)
SESSION_END = dtime(15, 30)
BLACKOUT_START = dtime(9, 30)
BLACKOUT_END = dtime(10, 0)

TRADING_DAYS = [0, 1, 2, 3, 4, 6]

EMA_PERIOD = 9
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

POINT_VALUES = {
    "NQ": 20.0,
    "ES": 50.0,
    "MNQ": 2.0,
    "MES": 5.0,
    "YM": 5.0,
    "RTY": 10.0,
}


def in_session() -> bool:
    now = datetime.now(ET)
    if now.weekday() not in TRADING_DAYS:
        return False
    t = now.time()
    if SESSION_START > SESSION_END:
        in_main = t >= SESSION_START or t < SESSION_END
    else:
        in_main = SESSION_START <= t < SESSION_END
    if not in_main:
        return False
    if BLACKOUT_START <= t < BLACKOUT_END:
        return False
    return True


# ============================================================
# ML Trade Filter (KNN-based pattern matching)
# ============================================================

ML_WARMUP_TRADES = 30
ML_K_NEIGHBORS = 7
ML_SKIP_THRESHOLD = 0.65

class TradeML:
    FEATURE_NAMES = [
        "direction",        # 1=long, -1=short
        "macd_hist",        # MACD histogram at entry
        "macd_momentum",    # histogram change (current - previous)
        "ema_distance",     # (price - EMA) / brick_size (normalized)
        "hour",             # hour of day ET (0-23)
        "volatility",       # std dev of last 10 brick closes / brick_size
        "breakout_strength", # how far price broke past range / brick_size
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
        print(f"[ML] Trade recorded: PnL=${pnl:.2f} | Total: {total} trades, {wins} wins ({100*wins/total:.0f}%)")

    def extract_features(self, direction: int, macd_hist: float, prev_macd_hist: float,
                         price: float, ema: float, brick_size: float,
                         brick_closes: list, range_high: float = 0, range_low: float = 0) -> dict:
        macd_momentum = (macd_hist - prev_macd_hist) if prev_macd_hist is not None else 0.0
        ema_distance = (price - ema) / brick_size if brick_size > 0 else 0.0
        hour = datetime.now(ET).hour

        vol = 0.0
        if len(brick_closes) >= 10:
            recent = brick_closes[-10:]
            vol = float(np.std(recent)) / brick_size if brick_size > 0 else 0.0

        breakout = 0.0
        if direction == 1 and range_high > 0:
            breakout = (price - range_high) / brick_size
        elif direction == -1 and range_low > 0:
            breakout = (range_low - price) / brick_size

        return {
            "direction": direction,
            "macd_hist": round(macd_hist, 4),
            "macd_momentum": round(macd_momentum, 4),
            "ema_distance": round(ema_distance, 4),
            "hour": hour,
            "volatility": round(vol, 4),
            "breakout_strength": round(breakout, 4),
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
        nearest_idx = np.argpartition(distances, k)[:k]
        nearest_wins = all_wins[nearest_idx]

        loss_ratio = 1.0 - nearest_wins.mean()

        skip = loss_ratio >= ML_SKIP_THRESHOLD
        reason = f"KNN({k}): {int(nearest_wins.sum())}/{k} wins, loss_ratio={loss_ratio:.2f}"
        return skip, loss_ratio, reason

    def _to_vector(self, features: dict) -> np.ndarray:
        return np.array([features.get(name, 0.0) for name in self.FEATURE_NAMES], dtype=float)

    def import_csv(self, csv_path: str, brick_size: float = 3.0):
        imported = 0
        try:
            with open(csv_path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pnl = float(row["PnL"])
                    entry_price = float(row["EntryPrice"])
                    exit_price = float(row["ExitPrice"])
                    direction = 1 if row["Type"] == "Long" else -1
                    entered_at = row["EnteredAt"]

                    hour = 12
                    try:
                        dt = datetime.fromisoformat(entered_at.replace(" -04:00", "-04:00").replace(" -05:00", "-05:00"))
                        hour = dt.hour
                    except Exception:
                        pass

                    features = {
                        "direction": direction,
                        "macd_hist": 0.0,
                        "macd_momentum": 0.0,
                        "ema_distance": direction * 1.0,
                        "hour": hour,
                        "volatility": 1.0,
                        "breakout_strength": abs(exit_price - entry_price) / brick_size if pnl > 0 else 0.1,
                    }
                    trade = {
                        "features": features,
                        "pnl": pnl,
                        "win": 1 if pnl > 0 else 0,
                        "source": "csv_import",
                        "timestamp": entered_at,
                    }
                    self.trades.append(trade)
                    imported += 1
        except Exception as e:
            print(f"[ML] CSV import error: {e}")

        if imported > 0:
            self._save()
            print(f"[ML] Imported {imported} trades from CSV")
        return imported

    def stats(self) -> str:
        if not self.trades:
            return "No trades recorded"
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"] == 1)
        losses = total - wins
        total_pnl = sum(t["pnl"] for t in self.trades)
        live = sum(1 for t in self.trades if t["source"] == "live")
        csv_count = sum(1 for t in self.trades if t["source"] == "csv_import")
        return (f"Total: {total} | Wins: {wins} | Losses: {losses} | "
                f"Win%: {100*wins/total:.0f}% | PnL: ${total_pnl:.2f} | "
                f"Live: {live} | CSV: {csv_count}")


# ============================================================
# Per-Symbol Strategy State
# ============================================================

class SymbolState:
    def __init__(self, symbol: str, brick_size: float, qty: int,
                 ntfy_topic: str, tg_token: str, tg_chat: str, tg_keys: list,
                 tick_interval: int = 10):
        self.symbol = symbol
        self.qty = qty
        self.brick_size = brick_size
        self.ntfy_topic = ntfy_topic
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys
        self.tick_interval = tick_interval
        self.last_tick_time = 0
        self.point_value = POINT_VALUES.get(symbol, 20.0)

        self.renko = RenkoEngine(brick_size, symbol)

        self.brick_closes = []

        self.ema = None
        self.prev_price_side = None  # "ABOVE" or "BELOW" based on live price vs EMA

        self.macd_fast_ema = None
        self.macd_slow_ema = None
        self.macd_line = None
        self.macd_signal_ema = None
        self.macd_hist = None
        self.prev_macd_hist = None

        self.pending_range_high = None
        self.pending_range_low = None

        self.last_price = 0.0

        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.entry_features = None
        self.sync_no_pos_count = 0

        self.live_pnl = 0.0

        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"trade_log_{symbol}.jsonl"
        )

        self.ml = None
        self.ctx = None
        self.last_new_bar_time = None

    def save_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "brick_closes": self.brick_closes[-100:],
            "ema": self.ema,
            "prev_price_side": self.prev_price_side,
            "macd_fast_ema": self.macd_fast_ema,
            "macd_slow_ema": self.macd_slow_ema,
            "macd_line": self.macd_line,
            "macd_signal_ema": self.macd_signal_ema,
            "macd_hist": self.macd_hist,
            "prev_macd_hist": self.prev_macd_hist,
            "last_price": self.last_price,
            "renko_last_close": self.renko.last_close,
            "renko_direction": self.renko.direction,
            "renko_brick_count": self.renko.brick_count,
            "position": self.position,
            "entry_price": self.entry_price,
            "live_pnl": self.live_pnl,
            "saved_at": time.time(),
        }

    def restore_state(self, state: dict):
        if time.time() - state.get("saved_at", 0) > 600:
            return False
        self.brick_closes = state.get("brick_closes", [])
        self.ema = state.get("ema")
        self.prev_price_side = state.get("prev_price_side")
        self.macd_fast_ema = state.get("macd_fast_ema")
        self.macd_slow_ema = state.get("macd_slow_ema")
        self.macd_line = state.get("macd_line")
        self.macd_signal_ema = state.get("macd_signal_ema")
        self.macd_hist = state.get("macd_hist")
        self.prev_macd_hist = state.get("prev_macd_hist")
        self.last_price = state.get("last_price", 0.0)
        self.renko.last_close = state.get("renko_last_close")
        self.renko.direction = state.get("renko_direction", 0)
        self.renko.brick_count = state.get("renko_brick_count", 0)
        self.position = state.get("position", 0)
        self.entry_price = state.get("entry_price", 0.0)
        self.live_pnl = state.get("live_pnl", 0.0)
        return True

    def _add_brick_data(self, brick_open, brick_close):
        self.brick_closes.append(brick_close)

    def _calc_indicators(self):
        n = len(self.brick_closes)
        c = self.brick_closes[-1]

        if n >= EMA_PERIOD:
            if self.ema is None:
                self.ema = sum(self.brick_closes[-EMA_PERIOD:]) / EMA_PERIOD
            else:
                k = 2.0 / (EMA_PERIOD + 1)
                self.ema = c * k + self.ema * (1 - k)

        k_fast = 2.0 / (MACD_FAST + 1)
        k_slow = 2.0 / (MACD_SLOW + 1)
        k_sig = 2.0 / (MACD_SIGNAL + 1)

        if n >= MACD_FAST and self.macd_fast_ema is None:
            self.macd_fast_ema = sum(self.brick_closes[-MACD_FAST:]) / MACD_FAST
        elif self.macd_fast_ema is not None:
            self.macd_fast_ema = c * k_fast + self.macd_fast_ema * (1 - k_fast)

        if n >= MACD_SLOW and self.macd_slow_ema is None:
            self.macd_slow_ema = sum(self.brick_closes[-MACD_SLOW:]) / MACD_SLOW
        elif self.macd_slow_ema is not None:
            self.macd_slow_ema = c * k_slow + self.macd_slow_ema * (1 - k_slow)

        if self.macd_fast_ema is not None and self.macd_slow_ema is not None:
            self.macd_line = self.macd_fast_ema - self.macd_slow_ema
            if self.macd_signal_ema is None:
                self.macd_signal_ema = self.macd_line
            else:
                self.macd_signal_ema = self.macd_line * k_sig + self.macd_signal_ema * (1 - k_sig)
            self.prev_macd_hist = self.macd_hist
            self.macd_hist = self.macd_line - self.macd_signal_ema

    async def seed_history(self):
        data = await self.ctx.data.get_data("1sec", bars=800)
        if data is None or len(data) == 0:
            print(f"[{self.symbol}] No historical 1sec data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[{self.symbol}] Seeding from {len(rows)} historical 1sec bars...")

        self.brick_closes = []
        self.ema = None
        self.prev_price_side = None
        self.macd_fast_ema = None
        self.macd_slow_ema = None
        self.macd_line = None
        self.macd_signal_ema = None
        self.macd_hist = None
        self.prev_macd_hist = None

        for row in rows:
            close = float(row["close"])
            bricks = self.renko.feed_close(close)
            for brick in bricks:
                self._add_brick_data(brick[0], brick[1])
                self._calc_indicators()

        if self.ema is not None and self.brick_closes:
            self.prev_price_side = "ABOVE" if self.brick_closes[-1] > self.ema else "BELOW"

        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"
        print(f"  [{self.symbol}] Renko: {self.renko.brick_count} bricks, {dir_str}, ref={self.renko.last_close:.2f}")
        print(f"  [{self.symbol}] Data: {len(self.brick_closes)} brick closes")
        ema_str = f"{self.ema:.2f}" if self.ema is not None else "N/A"
        side_str = self.prev_price_side or "N/A"
        macd_str = f"{self.macd_hist:.2f}" if self.macd_hist is not None else "N/A"
        print(f"  [{self.symbol}] EMA9: {ema_str} | Price vs EMA: {side_str} | MACD Hist: {macd_str}")

    def print_status(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"
        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"

        print(f"  [{self.symbol} @ {now}]")
        print(f"    Renko: {dir_str} | last_close={self.renko.last_close:.2f} | bricks={self.renko.brick_count}")
        ema_str = f"{self.ema:.2f}" if self.ema is not None else "N/A"
        side_str = self.prev_price_side or "N/A"
        macd_str = f"{self.macd_hist:.2f}" if self.macd_hist is not None else "N/A"
        print(f"    EMA9: {ema_str} | Price vs EMA: {side_str} | MACD Hist: {macd_str}")
        print(f"    Position: {pos_str} | P&L: ${self.live_pnl:.2f} | PV: ${self.point_value}/pt")

    def is_data_stale(self, threshold=120):
        """Check if we haven't received fresh bar data in threshold seconds."""
        if self.last_new_bar_time is None:
            return False
        return (time.time() - self.last_new_bar_time) > threshold

    async def tick(self):
        if self.ctx is None:
            return

        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        self.last_price = price

        now_ts = time.time()
        if now_ts - self.last_tick_time < self.tick_interval:
            return True
        self.last_tick_time = now_ts
        self.last_new_bar_time = now_ts

        # Position sync disabled: SDK get_all_positions() broken (contractDisplayName bug)
        # Bot relies on EMA exit signals; platform TP/SL acts as safety net

        # Feed Renko and update indicators on new bricks
        bricks = self.renko.feed_close(price)
        now = datetime.now(ET).strftime("%H:%M:%S")

        if bricks:
            for b in bricks:
                brick_dir = b[2]
                brick_color = "BULLISH" if brick_dir == 1 else "BEARISH"
                self._add_brick_data(b[0], b[1])
                self._calc_indicators()

                ema_str = f"EMA: {self.ema:.2f}" if self.ema is not None else "EMA: N/A"
                macd_h = f"MACD: {self.macd_hist:.2f}" if self.macd_hist is not None else "MACD: N/A"
                prev_h = f"prev: {self.prev_macd_hist:.2f}" if self.prev_macd_hist is not None else ""
                print(f"[{now}] [{self.symbol} RENKO] {brick_color} brick #{self.renko.brick_count}: {b[0]:.2f} -> {b[1]:.2f} | {ema_str} | {macd_h} {prev_h}")

        if self.ema is None:
            return True

        above_ema = price > self.ema
        below_ema = price < self.ema
        curr_side = "ABOVE" if above_ema else "BELOW"

        flipped = self.prev_price_side is not None and curr_side != self.prev_price_side
        self.prev_price_side = curr_side

        macd_rising = (self.macd_hist is not None and self.prev_macd_hist is not None
                       and self.macd_hist > self.prev_macd_hist)
        macd_falling = (self.macd_hist is not None and self.prev_macd_hist is not None
                        and self.macd_hist < self.prev_macd_hist)

        # Exit on price crossing EMA
        if flipped:
            if above_ema and self.position == -1:
                trade_pnl = (self.entry_price - price) * self.point_value * self.qty
                print(f"[{now}] [{self.symbol} EXIT] Price {price:.2f} crossed above EMA {self.ema:.2f} | Closing SHORT")
                if self.ml and self.entry_features:
                    self.ml.record_trade(self.entry_features, trade_pnl, source="bot_ema_exit")
                    self.entry_features = None
                await self._flatten(price, reason="EMA_CROSS")
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "FLAT", self.symbol, price, 0), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()

            elif below_ema and self.position == 1:
                trade_pnl = (price - self.entry_price) * self.point_value * self.qty
                print(f"[{now}] [{self.symbol} EXIT] Price {price:.2f} crossed below EMA {self.ema:.2f} | Closing LONG")
                if self.ml and self.entry_features:
                    self.ml.record_trade(self.entry_features, trade_pnl, source="bot_ema_exit")
                    self.entry_features = None
                await self._flatten(price, reason="EMA_CROSS")
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "FLAT", self.symbol, price, 0), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()

        # Cancel existing pending range if EMA crosses back before breakout
        if flipped and self.pending_range_high is not None:
            print(f"[{now}] [{self.symbol} RANGE] Cancelled - EMA crossed back")
            self.pending_range_high = None
            self.pending_range_low = None

        # On EMA cross while flat + MACD confirms: set up breakout range from the crossing brick
        if flipped and self.position == 0 and bricks:
            last_brick = bricks[-1]
            range_high = max(last_brick[0], last_brick[1])
            range_low = min(last_brick[0], last_brick[1])

            if above_ema and macd_rising:
                self.pending_range_high = range_high
                self.pending_range_low = range_low
                print(f"[{now}] [{self.symbol} RANGE] EMA cross + MACD rising | Waiting for breakout of {range_low:.2f} - {range_high:.2f}")
            elif below_ema and macd_falling:
                self.pending_range_high = range_high
                self.pending_range_low = range_low
                print(f"[{now}] [{self.symbol} RANGE] EMA cross + MACD falling | Waiting for breakout of {range_low:.2f} - {range_high:.2f}")
            elif above_ema:
                print(f"[{now}] [{self.symbol} SKIP] Cross above EMA but MACD not confirming ({self.macd_hist:.2f}) - no range set")
            elif below_ema:
                print(f"[{now}] [{self.symbol} SKIP] Cross below EMA but MACD not confirming ({self.macd_hist:.2f}) - no range set")

        # Check for breakout of pending range
        if self.position == 0 and self.pending_range_high is not None:
            if price > self.pending_range_high:
                rng_h = self.pending_range_high
                rng_l = self.pending_range_low
                # ML check before entry
                if self.ml:
                    features = self.ml.extract_features(
                        direction=1, macd_hist=self.macd_hist or 0,
                        prev_macd_hist=self.prev_macd_hist,
                        price=price, ema=self.ema, brick_size=self.brick_size,
                        brick_closes=self.brick_closes,
                        range_high=rng_h, range_low=rng_l)
                    skip, loss_ratio, reason = self.ml.should_skip(features)
                    if skip:
                        print(f"[{now}] [{self.symbol} ML-SKIP] LONG skipped | {reason}")
                        self.pending_range_high = None
                        self.pending_range_low = None
                        threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                            f"ML-SKIP|{self.symbol} LONG @ {price:.2f} | {reason}"), daemon=True).start()
                        return True
                    self.entry_features = features
                    print(f"[{now}] [{self.symbol} ML-OK] LONG approved | {reason}")
                print(f"[{now}] [{self.symbol} SIGNAL] LONG | price {price:.2f} broke above range {rng_h:.2f}")
                self.pending_range_high = None
                self.pending_range_low = None
                await self._enter_long(price)
            elif price < self.pending_range_low:
                rng_h = self.pending_range_high
                rng_l = self.pending_range_low
                # ML check before entry
                if self.ml:
                    features = self.ml.extract_features(
                        direction=-1, macd_hist=self.macd_hist or 0,
                        prev_macd_hist=self.prev_macd_hist,
                        price=price, ema=self.ema, brick_size=self.brick_size,
                        brick_closes=self.brick_closes,
                        range_high=rng_h, range_low=rng_l)
                    skip, loss_ratio, reason = self.ml.should_skip(features)
                    if skip:
                        print(f"[{now}] [{self.symbol} ML-SKIP] SHORT skipped | {reason}")
                        self.pending_range_high = None
                        self.pending_range_low = None
                        threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                            f"ML-SKIP|{self.symbol} SHORT @ {price:.2f} | {reason}"), daemon=True).start()
                        return True
                    self.entry_features = features
                    print(f"[{now}] [{self.symbol} ML-OK] SHORT approved | {reason}")
                print(f"[{now}] [{self.symbol} SIGNAL] SHORT | price {price:.2f} broke below range {rng_l:.2f}")
                self.pending_range_high = None
                self.pending_range_low = None
                await self._enter_short(price)

        return True

    async def _cleanup_ghost_position(self):
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=5.0)
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [{self.symbol}] Cleaned up ghost position (order filled despite timeout)")
        except Exception:
            pass

    async def _enter_long(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG @ {price:.2f} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await asyncio.wait_for(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=0,
                    size=self.qty,
                ),
                timeout=15.0,
            )
            if response.success:
                self.position = 1
                self.entry_price = price
                self.entry_time = datetime.now(ET)
                print(f"[{self.symbol}] Order filled. ID: {response.orderId}")
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "LONG", self.symbol, price, self.qty), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
            else:
                print(f"[{self.symbol}] Order FAILED: {response.errorMessage}")
                threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                    f"ALERT|{self.symbol} LONG order FAILED @ {price:.2f}"), daemon=True).start()
                return False
        except Exception as e:
            print(f"[{self.symbol}] Order ERROR: {e}")
            await self._cleanup_ghost_position()
            threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                f"ALERT|{self.symbol} LONG order ERROR @ {price:.2f} - reconnecting"), daemon=True).start()
            return False
        return True

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT @ {price:.2f} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await asyncio.wait_for(
                self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=1,
                    size=self.qty,
                ),
                timeout=15.0,
            )
            if response.success:
                self.position = -1
                self.entry_price = price
                self.entry_time = datetime.now(ET)
                print(f"[{self.symbol}] Order filled. ID: {response.orderId}")
                threading.Thread(target=send_signals, args=(
                    self.tg_token, self.tg_chat, self.tg_keys,
                    "SHORT", self.symbol, price, self.qty), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
            else:
                print(f"[{self.symbol}] Order FAILED: {response.errorMessage}")
                threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                    f"ALERT|{self.symbol} SHORT order FAILED @ {price:.2f}"), daemon=True).start()
                return False
        except Exception as e:
            print(f"[{self.symbol}] Order ERROR: {e}")
            await self._cleanup_ghost_position()
            threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                f"ALERT|{self.symbol} SHORT order ERROR @ {price:.2f} - reconnecting"), daemon=True).start()
            return False
        return True

    async def _flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        trade_pnl = (price - self.entry_price) * self.position * self.point_value * self.qty
        self.live_pnl += trade_pnl

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] <<< EXITING {direction} @ {price:.2f} | Trade: ${trade_pnl:+.2f} | P&L: ${self.live_pnl:.2f} | {reason}")

        saved_entry_price = self.entry_price
        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None

        try:
            result = await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id,
                ),
                timeout=5.0,
            )
            print(f"[{self.symbol}] Position closed via close_position_direct")
        except Exception as e:
            print(f"[{self.symbol}] close_position_direct failed ({e}) - likely already closed by TP/SL")

        self._log_trade(direction, saved_entry_price, price, trade_pnl, reason)
        return True

    def _log_trade(self, direction, entry_price, exit_price, pnl, reason):
        now = datetime.now(ET)
        trade = {
            "date": now.strftime("%Y-%m-%d"),
            "symbol": self.symbol,
            "entry_time": self.entry_time.strftime("%H:%M:%S") if self.entry_time else "N/A",
            "exit_time": now.strftime("%H:%M:%S"),
            "direction": direction,
            "entry": entry_price,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,
            "ema": self.ema,
            "macd_hist": self.macd_hist,
            "account": os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown"),
            "session_pnl": self.live_pnl,
        }
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(trade) + "\n")
        except Exception as e:
            print(f"[{self.symbol}] Trade log write error: {e}")


# ============================================================
# Main Bot (connection + session management)
# ============================================================

class RenkoBot:
    def __init__(self, symbol_configs: list, tg_token: str = "", tg_chat: str = "",
                 tg_keys: list = None, tick_interval: int = 10):
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tick_interval = tick_interval

        ml_data_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ml_trades.json"
        )
        self.ml = TradeML(ml_data_file)

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
            )
            state.ml = self.ml
            self.states[sym] = state

        self.was_in_session = False
        self.last_price_time = None
        self.connection_alive = True
        self.disconnect_alert_sent = False
        self.STALE_THRESHOLD = 60
        self.RECONNECT_THRESHOLD = 90
        self.reconnecting = False
        self.last_reconnect_time = 0
        self.last_status_notify = 0
        self.last_state_save = 0
        self.last_heartbeat = 0
        self.HEARTBEAT_INTERVAL = 1800  # 30 min
        self.last_gateway_logout_time = 0

        self.suite = None
        self.running = False
        self.state_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "bot_state.json"
        )

    def _symbols_list(self):
        return list(self.states.keys())

    def save_all_state(self):
        try:
            state = {sym: st.save_state() for sym, st in self.states.items()}
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except Exception:
            pass

    def load_all_state(self) -> bool:
        try:
            if not os.path.exists(self.state_file):
                return False
            with open(self.state_file) as f:
                saved = json.load(f)
            restored = False
            for sym, st in self.states.items():
                if sym in saved:
                    if st.restore_state(saved[sym]):
                        ema_s = f"{st.ema:.2f}" if st.ema is not None else "N/A"
                        print(f"  [{sym}] Restored: EMA={ema_s}, Bricks={st.renko.brick_count}")
                        restored = True
                    else:
                        print(f"  [{sym}] Saved state too old, seeding fresh")
            return restored
        except Exception:
            return False

    def _notify_status(self, msg):
        now_ts = time.time()
        if now_ts - self.last_status_notify > 300:
            send_telegram(self.tg_token, self.tg_chat, msg)
            self.last_status_notify = now_ts

    async def run(self):
        from project_x_py import TradingSuite

        symbols = self._symbols_list()
        print(f"[BOT] Renko EMA + MACD Strategy - LIVE MODE")
        print(f"[BOT] Tick interval: {self.tick_interval}s (samples price every {self.tick_interval} seconds)")
        print(f"[BOT] EMA({EMA_PERIOD}) + MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) on Close-based Renko")
        print(f"[BOT] Symbols: {', '.join(symbols)}")
        for sym, st in self.states.items():
            print(f"[BOT]   {sym}: brick={st.brick_size}, qty={st.qty}, pv=${st.point_value}/pt" +
                  (f", ntfy={st.ntfy_topic}" if st.ntfy_topic else ""))
        print(f"[BOT] SETUP: price crosses EMA + MACD confirms -> set breakout range from crossing brick")
        print(f"[BOT] ENTRY: price breaks above/below the range")
        print(f"[BOT] EXIT: price crosses back through EMA")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - {SESSION_END.strftime('%H:%M')} ET ({trading_day_str})")
        print(f"[BOT] Blackout: {BLACKOUT_START.strftime('%H:%M')} - {BLACKOUT_END.strftime('%H:%M')} ET (no trading)")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        print(f"[BOT] ML Filter: {self.ml.stats()}")
        print(f"[BOT] ML Settings: warmup={ML_WARMUP_TRADES}, K={ML_K_NEIGHBORS}, skip_threshold={ML_SKIP_THRESHOLD}")
        print()

        self.suite = await TradingSuite.create(
            instruments=symbols,
            timeframes=["1sec", "15min"],
            initial_days=1,
        )

        self._register_gateway_logout()

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")

        restored = self.load_all_state()

        for sym, st in self.states.items():
            st.ctx = self.suite[sym]
            print(f"[BOT] {sym} contract: {st.ctx.instrument_info.id}")

            price = await st.ctx.data.get_current_price()
            if price:
                if not restored or st.renko.last_close is None:
                    st.renko.initialize(price)
                st.last_price = price
                print(f"[BOT] {sym} price: {price:.2f}")

            if not restored or st.ema is None:
                await st.seed_history()
            else:
                print(f"  [{sym}] Using restored state (skipping seed)")

        # Close any orphan positions from previous crashes to prevent double-entry
        for sym, st in self.states.items():
            if st.ctx:
                try:
                    result = await asyncio.wait_for(
                        st.ctx.positions.close_position_direct(
                            contract_id=st.ctx.instrument_info.id,
                        ),
                        timeout=5.0,
                    )
                    if result and result.get("success"):
                        print(f"  [{sym}] Closed orphan position on startup (ID: {result.get('orderId')})")
                        send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                     "FLAT", sym, st.last_price, 0, ntfy_topic=st.ntfy_topic)
                except Exception:
                    pass
            st.position = 0
            st.entry_price = 0.0

        print()
        self.running = True
        self.was_in_session = in_session()

        for st in self.states.values():
            st.print_status()

        print(f"\n[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Trading LIVE - EMA + MACD ({', '.join(symbols)})")
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

    def _register_gateway_logout(self):
        try:
            conn = self.suite.realtime.user_connection
            def on_logout(*args):
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] [LOGOUT] GatewayLogout received from TopstepX")
                self.last_gateway_logout_time = time.time()
            conn.on("GatewayLogout", on_logout)
        except Exception as e:
            print(f"[WARN] Could not register GatewayLogout handler: {e}")

    async def _auto_reconnect(self):
        from project_x_py import TradingSuite
        self.reconnecting = True
        self.last_reconnect_time = time.time()
        now = datetime.now(ET).strftime("%H:%M:%S")
        symbols = self._symbols_list()
        print(f"[{now}] [RECONNECT] Auto-reconnecting (indicators preserved)...")
        self._notify_status(f"STATUS|Auto-reconnecting ({now} ET)")

        if self.suite:
            try:
                await self.suite.disconnect()
            except Exception:
                pass

        try:
            self.suite = await TradingSuite.create(
                instruments=symbols,
                timeframes=["1sec", "15min"],
                initial_days=1,
            )
            for sym, st in self.states.items():
                st.ctx = self.suite[sym]

            self._register_gateway_logout()
            self.last_price_time = time.time()
            self.connection_alive = True
            self.disconnect_alert_sent = False
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] WebSocket restored, indicators intact")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|RECONNECTED ({now} ET)")

            for sym, st in self.states.items():
                if st.position != 0:
                    direction = "LONG" if st.position == 1 else "SHORT"
                    print(f"[{now}] [SAFETY] {sym} was {direction} during disconnect - FLATTENING")
                    send_telegram(self.tg_token, self.tg_chat,
                                 f"STATUS|SAFETY FLATTEN {sym} - was {direction} ({now} ET)")
                    try:
                        price = await st.ctx.data.get_current_price()
                        if price:
                            await st._flatten(price, reason="SAFETY_RECONNECT")
                            send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                         "FLAT", sym, price, 0, ntfy_topic=st.ntfy_topic)
                    except Exception as e:
                        print(f"[{now}] [SAFETY] {sym} flatten failed: {e}")

        except Exception as e:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] Failed: {e} - will retry in 2 min")
            self.suite = None
            for st in self.states.values():
                st.ctx = None
        finally:
            self.reconnecting = False

    async def _tick(self):
        if self.suite is None:
            if in_session() and not self.reconnecting:
                if time.time() - self.last_reconnect_time > 120:
                    await self._auto_reconnect()
            return

        # Check price health using first symbol
        first_state = next(iter(self.states.values()))
        price = await first_state.ctx.data.get_current_price() if first_state.ctx else None
        now_ts = time.time()

        if price is None:
            if self.last_price_time and in_session():
                elapsed = now_ts - self.last_price_time
                if elapsed > self.STALE_THRESHOLD and not self.disconnect_alert_sent:
                    self.connection_alive = False
                    self.disconnect_alert_sent = True
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [ALERT] No price data for {int(elapsed)}s")
                    self._notify_status(f"STATUS|DISCONNECTED ({now} ET)")
                if elapsed > self.RECONNECT_THRESHOLD and not self.reconnecting:
                    if now_ts - self.last_reconnect_time > 120:
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
                    print(f"[SESSION] {sym} - Session ended - flattening")
                    p = await st.ctx.data.get_current_price()
                    if p:
                        await st._flatten(p, reason="SESSION_END")
                        send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                     "FLAT", sym, p, 0, ntfy_topic=st.ntfy_topic)
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] Disconnecting until next session...")
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
                print(f"[{now_str}] [SESSION] Reconnecting...")
                self.suite = await TradingSuite.create(
                    instruments=symbols,
                    timeframes=["1sec", "15min"],
                    initial_days=1,
                )
                for sym, st in self.states.items():
                    st.ctx = self.suite[sym]
                    await st.seed_history()
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] New session started - LIVE")
            for st in self.states.values():
                st.print_status()

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # Tick each symbol's strategy
        order_failed = False
        for st in self.states.values():
            result = await st.tick()
            if result is False:
                order_failed = True

        if order_failed and not self.reconnecting:
            if time.time() - self.last_reconnect_time > 120:
                await self._auto_reconnect()

        # Stale data detection: if any symbol hasn't received new bar data in 2 min, reconnect
        if not self.reconnecting:
            for sym, st in self.states.items():
                if st.is_data_stale(max(120, st.tick_interval * 3)):
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [STALE] {sym} no new data for 2+ min - reconnecting")
                    self._notify_status(f"STATUS|{sym} data stale, reconnecting ({now} ET)")
                    if time.time() - self.last_reconnect_time > 120:
                        await self._auto_reconnect()
                    break

        if time.time() - self.last_state_save > 30:
            self.save_all_state()
            self.last_state_save = time.time()

        # Handle GatewayLogout: TopstepX killed our session, need to reconnect
        if self.last_gateway_logout_time > 0 and time.time() - self.last_gateway_logout_time > 5:
            self.last_gateway_logout_time = 0
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [LOGOUT] GatewayLogout detected - reconnecting")
            self._notify_status(f"STATUS|GatewayLogout - reconnecting ({now} ET)")
            if time.time() - self.last_reconnect_time > 120:
                await self._auto_reconnect()

        # Heartbeat: send Telegram status every 30 min so client knows bot is alive
        if time.time() - self.last_heartbeat > self.HEARTBEAT_INTERVAL:
            self.last_heartbeat = time.time()
            now = datetime.now(ET).strftime("%H:%M:%S")
            for sym, st in self.states.items():
                pos_str = "FLAT" if st.position == 0 else ("LONG" if st.position == 1 else "SHORT")
                range_str = ""
                if st.pending_range_high is not None:
                    range_str = f" | Range: {st.pending_range_low:.2f}-{st.pending_range_high:.2f}"
                ema_str = f"{st.ema:.2f}" if st.ema else "N/A"
                msg = f"HEARTBEAT|{sym} alive ({now} ET) | {pos_str} | P&L: ${st.live_pnl:.2f} | EMA: {ema_str}{range_str}"
                threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat, msg), daemon=True).start()

    async def _shutdown(self):
        self.save_all_state()
        print("\n[BOT] Shutdown (state saved)...")
        for sym, st in self.states.items():
            if st.position != 0 and st.ctx:
                try:
                    price = await asyncio.wait_for(
                        st.ctx.data.get_current_price(), timeout=3.0
                    )
                    if price:
                        await asyncio.wait_for(
                            st._flatten(price, reason="SHUTDOWN"), timeout=5.0
                        )
                        send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                     "FLAT", sym, price, 0, ntfy_topic=st.ntfy_topic)
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
    """Parse 'NQ:3.0:1:ntfy-topic,ES:2.0:1' into list of config dicts."""
    configs = []
    for part in symbols_str.split(","):
        parts = part.strip().split(":")
        if len(parts) < 3:
            raise ValueError(f"Invalid symbol config '{part}'. Format: SYMBOL:BRICK_SIZE:QTY[:NTFY_TOPIC]")
        cfg = {
            "symbol": parts[0].strip().upper(),
            "brick_size": float(parts[1]),
            "qty": int(parts[2]),
            "ntfy_topic": parts[3].strip() if len(parts) > 3 else "",
        }
        configs.append(cfg)
    return configs


def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko AO Color Bot (Multi-Symbol)")
    parser.add_argument("--symbol", default="", help="Single symbol (backward compat)")
    parser.add_argument("--symbols", default="", help="Multi-symbol config: 'NQ:3.0:1:ntfy,ES:2.0:1'")
    parser.add_argument("--qty", type=int, default=1, help="Qty for single --symbol mode")
    parser.add_argument("--brick-size", type=float, default=1.0, help="Brick size for single --symbol mode")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--ntfy-topic", default="", help="ntfy.sh topic (single --symbol mode)")
    parser.add_argument("--tick-interval", type=int, default=10, help="Seconds between price samples (default: 10)")
    parser.add_argument("--import-csv", default="", help="Import trades CSV for ML training")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []

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
        symbol_configs = [{"symbol": "NQ", "brick_size": 1.0, "qty": 1, "ntfy_topic": ""}]

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

    # Truncate log file if too large (prevents disk fill from crash loops)
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 10_000_000:
            os.truncate(log_file, 0)
            print(f"[BOT] Log file truncated (was > 10MB)")
    except Exception:
        pass

    csv_imported = False
    while not stopped:
        bot = RenkoBot(
            symbol_configs=symbol_configs,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            tick_interval=args.tick_interval,
        )
        if args.import_csv and not csv_imported and len(bot.ml.trades) == 0:
            brick_size = symbol_configs[0].get("brick_size", 3.0) if symbol_configs else 3.0
            bot.ml.import_csv(args.import_csv, brick_size=brick_size)
            csv_imported = True
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
            print(f"\n[{now}] [CRASH] Bot crashed: {type(e).__name__}: {e}")
            print(f"[{now}] [CRASH] Restarting in {retry_delay}s...")
            if time.time() - last_crash_notify > CRASH_NOTIFY_COOLDOWN:
                send_telegram(args.tg_token, args.tg_chat, f"STATUS|Bot crashed, restarting in {retry_delay}s ({now} ET)")
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
