"""
TopstepX Renko Crossover + Bollinger Band Bot (LIVE)
Multi-symbol support: runs multiple instruments on one connection.

Strategy: Renko Stop-and-Go Crossover with BB Filter
- SMA(12) of renko brick opens = renko MA line
- SMA(2) of brick closes = smoothed price
- When smoothed price crosses renko MA → signal
- BB(20,2) filter: only enter if >= 5pts room to target band
  - LONG: need room to upper BB | SHORT: need room to lower BB
- EXIT: opposite crossover signal flips the position (no fixed SL/TP)

Usage:
    python renko_bot.py --symbols "NQ:5:1:ntfy-topic" --tick-interval 1
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
        if self.brick_size <= 0:
            self.last_close = price
            return
        self.last_close = round(price / self.brick_size) * self.brick_size

    def feed_close(self, close_price: float) -> list:
        if self.last_close is None:
            self.initialize(close_price)
            return []

        if self.brick_size <= 0:
            return []

        new_bricks = []
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
            print(f"[RENKO {self.label}] WARNING: hit safety cap of {MAX_BRICKS_PER_FEED} bricks - input price={close_price}, brick_size={self.brick_size}")
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

SESSION_START = dtime(0, 0, 0)
SESSION_END = dtime(23, 59, 59)
BLACKOUT_START = dtime(0, 0)
BLACKOUT_END = dtime(0, 0)

TRADING_DAYS = [0, 1, 2, 3, 4, 5, 6]

EMA_PERIOD = 9
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

RENKO_SMA_PERIOD = 12
PRICE_SMOOTH_PERIOD = 2
BB_PERIOD = 20
BB_STDDEV = 2.0
MIN_BB_ROOM = 5.0

RSG_DECAY_MULTIPLIER = 250
RSG_DETECTION = 1

MINTICK_VALUES = {
    "NQ": 0.25, "ES": 0.25, "MNQ": 0.25, "MES": 0.25,
    "YM": 1.0, "RTY": 0.10,
}

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
    return in_main


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
        self.brick_opens = []

        self.ema = None
        self.prev_price_side = None

        self.rsg_decay = RSG_DECAY_MULTIPLIER * MINTICK_VALUES.get(symbol, 0.25)
        self.rsg_dosc = None
        self.rsg_dosc_values = []
        self.renko_sma = None
        self.bb_middle = None
        self.bb_upper = None
        self.bb_lower = None
        self.prev_smooth_vs_rma = None
        self.tp_target = None

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

        self.prev_live_side = None
        self.last_exit_time = 0
        self.ENTRY_COOLDOWN = 60

        self.prev_brick_direction = None

        # Connection / freshness tracking
        self.last_new_bar_time = None
        self.last_price_change_time = None
        self.last_known_price = None
        # Order error state: "ok" | "rejected" | "timeout" | "error" | None
        self.last_order_error = None

        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"trade_log_{symbol}.jsonl"
        )

        self.ml = None
        self.ctx = None

        self.last_position_sync_time = 0
        self.POSITION_SYNC_INTERVAL = 30

    def save_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "brick_closes": self.brick_closes[-100:],
            "brick_opens": self.brick_opens[-100:],
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
            "prev_brick_direction": self.prev_brick_direction,
            "prev_smooth_vs_rma": self.prev_smooth_vs_rma,
            "tp_target": self.tp_target,
            "rsg_dosc": self.rsg_dosc,
            "rsg_dosc_values": self.rsg_dosc_values[-100:],
            "saved_at": time.time(),
        }

    def restore_state(self, state: dict):
        if time.time() - state.get("saved_at", 0) > 600:
            return False
        self.brick_closes = state.get("brick_closes", [])
        self.brick_opens = state.get("brick_opens", [])
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
        self.prev_brick_direction = state.get("prev_brick_direction")
        self.prev_smooth_vs_rma = state.get("prev_smooth_vs_rma")
        self.tp_target = state.get("tp_target")
        self.rsg_dosc = state.get("rsg_dosc")
        self.rsg_dosc_values = state.get("rsg_dosc_values", [])
        if not self.rsg_dosc_values and self.brick_closes and self.brick_opens:
            for i in range(len(self.brick_closes)):
                bo = self.brick_opens[i] if i < len(self.brick_opens) else self.brick_closes[i]
                bc = self.brick_closes[i]
                self._update_rsg(bo, bc)
        if self.rsg_dosc_values and len(self.rsg_dosc_values) >= RENKO_SMA_PERIOD:
            self.renko_sma = sum(self.rsg_dosc_values[-RENKO_SMA_PERIOD:]) / RENKO_SMA_PERIOD
        if self.brick_closes and len(self.brick_closes) >= BB_PERIOD:
            bb_data = self.brick_closes[-BB_PERIOD:]
            self.bb_middle = sum(bb_data) / BB_PERIOD
            variance = sum((x - self.bb_middle) ** 2 for x in bb_data) / BB_PERIOD
            std_dev = math.sqrt(variance)
            self.bb_upper = self.bb_middle + BB_STDDEV * std_dev
            self.bb_lower = self.bb_middle - BB_STDDEV * std_dev
        return True

    def _add_brick_data(self, brick_open, brick_close):
        self.brick_closes.append(brick_close)
        self.brick_opens.append(brick_open)
        self._update_rsg(brick_open, brick_close)

    def _update_rsg(self, brick_open, brick_close):
        hh = max(brick_open, brick_close)
        ll = min(brick_open, brick_close)
        rprice = round(brick_close / self.rsg_decay) * self.rsg_decay

        if self.rsg_dosc is None:
            self.rsg_dosc = rprice
        else:
            predosc = self.rsg_dosc
            if hh > predosc + self.rsg_decay:
                self.rsg_dosc = rprice
            elif ll < predosc - self.rsg_decay:
                self.rsg_dosc = rprice

        self.rsg_dosc_values.append(self.rsg_dosc)

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

        if len(self.rsg_dosc_values) >= RENKO_SMA_PERIOD:
            self.renko_sma = sum(self.rsg_dosc_values[-RENKO_SMA_PERIOD:]) / RENKO_SMA_PERIOD

        if n >= BB_PERIOD:
            bb_data = self.brick_closes[-BB_PERIOD:]
            self.bb_middle = sum(bb_data) / BB_PERIOD
            variance = sum((x - self.bb_middle) ** 2 for x in bb_data) / BB_PERIOD
            std_dev = math.sqrt(variance)
            self.bb_upper = self.bb_middle + BB_STDDEV * std_dev
            self.bb_lower = self.bb_middle - BB_STDDEV * std_dev

    async def seed_history(self):
        data = await self.ctx.data.get_data("1sec", bars=800)
        if data is None or len(data) == 0:
            print(f"[{self.symbol}] No historical 1sec data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[{self.symbol}] Seeding from {len(rows)} historical 1sec bars...")

        self.brick_closes = []
        self.brick_opens = []
        self.ema = None
        self.prev_price_side = None
        self.macd_fast_ema = None
        self.macd_slow_ema = None
        self.macd_line = None
        self.macd_signal_ema = None
        self.macd_hist = None
        self.prev_macd_hist = None
        self.rsg_dosc = None
        self.rsg_dosc_values = []
        self.renko_sma = None
        self.bb_middle = None
        self.bb_upper = None
        self.bb_lower = None
        self.prev_smooth_vs_rma = None

        for row in rows:
            close = float(row["close"])
            bricks = self.renko.feed_close(close)
            for brick in bricks:
                self._add_brick_data(brick[0], brick[1])
                self._calc_indicators()

        if self.renko_sma is not None and len(self.brick_closes) >= PRICE_SMOOTH_PERIOD:
            smooth_price = sum(self.brick_closes[-PRICE_SMOOTH_PERIOD:]) / PRICE_SMOOTH_PERIOD
            self.prev_smooth_vs_rma = "ABOVE" if smooth_price > self.renko_sma else "BELOW"

        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"
        print(f"  [{self.symbol}] Renko: {self.renko.brick_count} bricks, {dir_str}, ref={self.renko.last_close:.2f}")
        print(f"  [{self.symbol}] Data: {len(self.brick_closes)} brick closes, {len(self.brick_opens)} brick opens")
        rma_str = f"{self.renko_sma:.2f}" if self.renko_sma is not None else "N/A"
        bb_str = f"Upper={self.bb_upper:.2f} Mid={self.bb_middle:.2f} Lower={self.bb_lower:.2f}" if self.bb_upper is not None else "N/A"
        print(f"  [{self.symbol}] RMA(12): {rma_str} | BB(20,2): {bb_str}")

    def print_status(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"
        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"

        print(f"  [{self.symbol} @ {now}]")
        print(f"    Renko: {dir_str} | last_close={self.renko.last_close:.2f} | bricks={self.renko.brick_count}")
        rma_str = f"{self.renko_sma:.2f}" if self.renko_sma is not None else "N/A"
        cross_str = self.prev_smooth_vs_rma or "N/A"
        print(f"    Renko SMA(12): {rma_str} | Smooth vs RMA: {cross_str}")
        bb_u = f"{self.bb_upper:.2f}" if self.bb_upper is not None else "N/A"
        bb_m = f"{self.bb_middle:.2f}" if self.bb_middle is not None else "N/A"
        bb_l = f"{self.bb_lower:.2f}" if self.bb_lower is not None else "N/A"
        print(f"    BB(20,2): Upper={bb_u} | Mid={bb_m} | Lower={bb_l}")
        tp_str = f"{self.tp_target:.2f}" if self.tp_target is not None else "N/A"
        print(f"    Position: {pos_str} | TP: {tp_str} | P&L: ${self.live_pnl:.2f} | PV: ${self.point_value}/pt")

    def is_data_stale(self, threshold=120):
        if self.last_new_bar_time is None:
            return False
        return (time.time() - self.last_new_bar_time) > threshold

    def is_price_frozen(self, threshold=90):
        if self.last_price_change_time is None:
            return False
        return (time.time() - self.last_price_change_time) > threshold

    async def _try_get_real_position(self):
        """
        Query the real open position from TopstepX.
        Returns int > 0 (long), < 0 (short), 0 (flat), or None (query failed).
        None means caller must NOT assume flat.
        """
        if self.ctx is None:
            return None
        try:
            positions = await asyncio.wait_for(
                self.ctx.positions.get_all_positions(), timeout=3.0
            )
            cid = self.ctx.instrument_info.id
            for p in (positions or []):
                try:
                    p_cid = (getattr(p, "contract_id", None)
                             or getattr(p, "contractId", None))
                    if p_cid == cid:
                        size = float(getattr(p, "size", 0)
                                     or getattr(p, "net_pos", 0) or 0)
                        side = getattr(p, "side", 0)
                        return int(size) if side == 0 else -int(size)
                except Exception:
                    continue
            return 0
        except Exception as e:
            print(f"[{self.symbol}] get_all_positions error: {e}")
            return None

    async def sync_position_with_platform(self) -> bool:
        """
        Disabled: SDK contractDisplayName bug makes get_all_positions() silently
        return 0 positions when positions exist, causing every trade to be
        falsely detected as "closed externally" and reset to flat.
        """
        return False

    async def _sync_position_with_platform_DISABLED(self) -> bool:
        if self.ctx is None or self.position == 0:
            return False

        now_ts = time.time()
        if now_ts - self.last_position_sync_time < self.POSITION_SYNC_INTERVAL:
            return False
        self.last_position_sync_time = now_ts

        real_pos = await self._try_get_real_position()
        if real_pos is None:
            return False

        if real_pos == 0 and self.position != 0:
            direction = "LONG" if self.position == 1 else "SHORT"
            now = datetime.now(ET).strftime("%H:%M:%S")
            price = self.last_price or 0.0
            pnl_est = (
                (price - self.entry_price) * self.position
                * self.point_value * self.qty
            ) if self.entry_price else 0.0

            print(
                f"\n[{now}] [{self.symbol}] SYNC: Platform is FLAT but bot "
                f"thought {direction} - closed externally "
                f"(TP/SL / manual close / auto-liquidation). "
                f"Est PnL: ${pnl_est:+.2f}. Correcting state."
            )

            saved_entry_price = self.entry_price
            saved_entry_time = self.entry_time

            if self.ml and self.entry_features:
                self.ml.record_trade(self.entry_features, pnl_est,
                                     source="platform_closed")
            self.entry_features = None

            self.live_pnl += pnl_est
            self.position = 0
            self.entry_price = 0.0
            self.entry_time = None
            self._log_trade(direction, saved_entry_price, price, pnl_est,
                            "PLATFORM_CLOSED", saved_entry_time)

            threading.Thread(
                target=send_telegram,
                args=(
                    self.tg_token, self.tg_chat,
                    f"SYNC {self.symbol}: {direction} position was closed "
                    f"externally (TP/SL / manual / auto-liq).\n"
                    f"Est PnL: ${pnl_est:+.2f}\nBot state corrected.",
                ),
                daemon=True,
            ).start()
            return True

        return False

    async def tick(self):
        if self.ctx is None:
            return True

        price = await self.ctx.data.get_current_price()
        if price is None:
            return True

        # Track price-change for frozen-feed detection (independent of tick gating)
        if self.last_known_price is None or price != self.last_known_price:
            self.last_known_price = price
            self.last_price_change_time = time.time()
        self.last_price = price

        now_ts = time.time()

        # Feed Renko once per second (matching TV's 1s candle close, not every tick)
        if not hasattr(self, '_last_renko_feed_time') or now_ts - self._last_renko_feed_time >= 1.0:
            bricks = self.renko.feed_close(price)
            self._last_renko_feed_time = now_ts
        else:
            bricks = []
        now = datetime.now(ET).strftime("%H:%M:%S")

        flipped = False
        flip_above = False
        flip_below = False
        flip_brick = None

        if bricks:
            self.last_new_bar_time = now_ts
            entry_signal = None

            for b in bricks:
                brick_dir = b[2]
                brick_color = "BULLISH" if brick_dir == 1 else "BEARISH"

                self._add_brick_data(b[0], b[1])
                self._calc_indicators()

                rma_str = f"RMA: {self.renko_sma:.2f}" if self.renko_sma is not None else "RMA: N/A"
                bb_str = ""
                if self.bb_upper is not None:
                    bb_str = f" | BB: {self.bb_lower:.2f}/{self.bb_middle:.2f}/{self.bb_upper:.2f}"
                print(f"[{now}] [{self.symbol} RENKO] {brick_color} brick #{self.renko.brick_count}: {b[0]:.2f} -> {b[1]:.2f} | {rma_str}{bb_str}")

                if self.renko_sma is not None and len(self.brick_closes) >= PRICE_SMOOTH_PERIOD:
                    smooth_price = sum(self.brick_closes[-PRICE_SMOOTH_PERIOD:]) / PRICE_SMOOTH_PERIOD
                    if smooth_price > self.renko_sma:
                        cur_side = "ABOVE"
                    elif smooth_price < self.renko_sma:
                        cur_side = "BELOW"
                    else:
                        cur_side = self.prev_smooth_vs_rma

                    if self.prev_smooth_vs_rma is not None and cur_side != self.prev_smooth_vs_rma:
                        if cur_side == "ABOVE":
                            entry_signal = "LONG"
                            print(f"[{now}] [{self.symbol} CROSS] Bullish crossover | Smooth {smooth_price:.2f} crossed ABOVE RMA {self.renko_sma:.2f}")
                        elif cur_side == "BELOW":
                            entry_signal = "SHORT"
                            print(f"[{now}] [{self.symbol} CROSS] Bearish crossover | Smooth {smooth_price:.2f} crossed BELOW RMA {self.renko_sma:.2f}")

                    self.prev_smooth_vs_rma = cur_side

                self.prev_brick_direction = brick_dir

            if entry_signal is not None and self.bb_upper is not None:
                if self.position != 0:
                    old_dir = "LONG" if self.position == 1 else "SHORT"
                    trade_pnl = ((price - self.entry_price) if self.position == 1 else (self.entry_price - price)) * self.point_value * self.qty
                    print(f"[{now}] [{self.symbol} FLIP] Closing {old_dir} for {entry_signal} | Trade: ${trade_pnl:+.2f}")
                    if self.ml and self.entry_features:
                        self.ml.record_trade(self.entry_features, trade_pnl, source="signal_flip")
                        self.entry_features = None
                    await self._flatten(price, reason="SIGNAL_FLIP")
                    self.last_exit_time = 0

                if entry_signal == "LONG":
                    room = self.bb_upper - price
                    if room >= MIN_BB_ROOM:
                        print(f"[{now}] [{self.symbol} ENTRY] LONG | Room to upper BB: {room:.1f}pts (>= {MIN_BB_ROOM})")
                        await self._enter_long(price)
                    else:
                        print(f"[{now}] [{self.symbol} BB FILTER] LONG skipped - only {room:.1f}pts room to upper BB {self.bb_upper:.2f} (need {MIN_BB_ROOM})")
                elif entry_signal == "SHORT":
                    room = price - self.bb_lower
                    if room >= MIN_BB_ROOM:
                        print(f"[{now}] [{self.symbol} ENTRY] SHORT | Room to lower BB: {room:.1f}pts (>= {MIN_BB_ROOM})")
                        await self._enter_short(price)
                    else:
                        print(f"[{now}] [{self.symbol} BB FILTER] SHORT skipped - only {room:.1f}pts room to lower BB {self.bb_lower:.2f} (need {MIN_BB_ROOM})")
            elif entry_signal is not None and self.bb_upper is None:
                print(f"[{now}] [{self.symbol} WAIT] {entry_signal} signal but BB not ready (need {BB_PERIOD} bricks)")

        if self.renko_sma is None:
            return True

        # Position sync (30s interval)
        if now_ts - self.last_position_sync_time >= self.POSITION_SYNC_INTERVAL:
            await self.sync_position_with_platform()

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

    async def _ensure_flat_before_entry(self):
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=5.0)
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [{self.symbol}] Pre-entry safety: cleared any unknown position")
        except Exception:
            pass

    async def _enter_long(self, price: float):
        if self.position != 0:
            now = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            print(f"[{now}] [{self.symbol}] BLOCKED LONG entry - already in {direction} (max 1 position)")
            return False
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG @ {price:.2f} | P&L: ${self.live_pnl:.2f}")

        await self._ensure_flat_before_entry()

        async def _do_order():
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
                    return "ok"
                else:
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
                "LONG", self.symbol, price, self.qty), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
            return True
        elif result == "rejected":
            threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                f"ALERT|{self.symbol} LONG order REJECTED @ {price:.2f}"), daemon=True).start()
            return False
        else:
            await self._cleanup_ghost_position()
            threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                f"ALERT|{self.symbol} LONG order {result.upper()} @ {price:.2f} - reconnecting"), daemon=True).start()
            return False

    async def _enter_short(self, price: float):
        if self.position != 0:
            now = datetime.now(ET).strftime("%H:%M:%S")
            direction = "LONG" if self.position == 1 else "SHORT"
            print(f"[{now}] [{self.symbol}] BLOCKED SHORT entry - already in {direction} (max 1 position)")
            return False
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT @ {price:.2f} | P&L: ${self.live_pnl:.2f}")

        await self._ensure_flat_before_entry()

        async def _do_order():
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
                    return "ok"
                else:
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
                "SHORT", self.symbol, price, self.qty), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
            return True
        elif result == "rejected":
            threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                f"ALERT|{self.symbol} SHORT order REJECTED @ {price:.2f}"), daemon=True).start()
            return False
        else:
            await self._cleanup_ghost_position()
            threading.Thread(target=send_telegram, args=(self.tg_token, self.tg_chat,
                f"ALERT|{self.symbol} SHORT order {result.upper()} @ {price:.2f} - reconnecting"), daemon=True).start()
            return False

    async def _flatten(self, price: float, reason: str = ""):
        if self.position == 0:
            return True

        direction = "LONG" if self.position == 1 else "SHORT"
        saved_entry_price = self.entry_price
        saved_position = self.position
        saved_qty = self.qty

        trade_pnl = (price - saved_entry_price) * saved_position * self.point_value * saved_qty

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] <<< EXITING {direction} @ {price:.2f} | Trade: ${trade_pnl:+.2f} | P&L (est): ${self.live_pnl + trade_pnl:.2f} | {reason}")

        async def _do_close():
            try:
                await asyncio.wait_for(
                    self.ctx.positions.close_position_direct(
                        contract_id=self.ctx.instrument_info.id,
                    ),
                    timeout=5.0,
                )
                print(f"[{self.symbol}] Position closed via close_position_direct")
                return True
            except asyncio.TimeoutError:
                print(f"[{self.symbol}] close_position_direct TIMEOUT - assuming closed (TP/SL may have hit)")
                return True
            except Exception as ex:
                print(f"[{self.symbol}] close_position_direct failed ({ex}) - likely already closed by TP/SL")
                return True

        close_ok = await asyncio.shield(_do_close())

        if close_ok:
            saved_entry_time = self.entry_time
            self.live_pnl += trade_pnl
            self.position = 0
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
        self.reconnect_failures = 0
        self.reconnect_cooldown = 5
        self.RECONNECT_COOLDOWN_OK = 30
        self.RECONNECT_COOLDOWN_MAX = 120
        self.last_status_notify = 0
        self.last_state_save = 0
        self.last_heartbeat = 0
        self.HEARTBEAT_INTERVAL = 1800
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
        print(f"[BOT] ENTRY: ghost brick (live price) crosses EMA")
        print(f"[BOT] EXIT: price crosses EMA opposite direction OR stop loss (1 brick = {list(self.states.values())[0].brick_size} pts)")
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

            def on_position_ws(*args):
                for arg in args:
                    try:
                        cid = (getattr(arg, "contract_id", None)
                               or getattr(arg, "contractId", None))
                        size = float(getattr(arg, "size", 0)
                                     or getattr(arg, "net_pos", 0) or 0)
                        if cid is None:
                            continue
                        for sym, st in self.states.items():
                            if (st.ctx
                                    and st.ctx.instrument_info.id == cid
                                    and st.position != 0
                                    and abs(size) < 0.01):
                                direction = "LONG" if st.position == 1 else "SHORT"
                                now_str = datetime.now(ET).strftime("%H:%M:%S")
                                price = st.last_price or 0.0
                                pnl_est = (
                                    (price - st.entry_price) * st.position
                                    * st.point_value * st.qty
                                ) if st.entry_price else 0.0
                                print(
                                    f"[{now_str}] [{sym}] WS-SYNC: "
                                    f"{direction} closed externally via platform event. "
                                    f"Est PnL: ${pnl_est:+.2f}"
                                )
                                saved_ep = st.entry_price
                                saved_et = st.entry_time
                                if st.ml and st.entry_features:
                                    st.ml.record_trade(st.entry_features, pnl_est,
                                                       source="ws_platform_closed")
                                st.entry_features = None
                                st.live_pnl += pnl_est
                                st.position = 0
                                st.entry_price = 0.0
                                st.entry_time = None
                                st._log_trade(direction, saved_ep, price, pnl_est,
                                              "WS_PLATFORM_CLOSED", saved_et)
                                threading.Thread(
                                    target=send_telegram,
                                    args=(
                                        self.tg_token, self.tg_chat,
                                        f"WS-SYNC {sym}: {direction} closed externally. "
                                        f"Est PnL: ${pnl_est:+.2f}. Bot state corrected.",
                                    ),
                                    daemon=True,
                                ).start()
                                threading.Thread(
                                    target=send_signals,
                                    args=(self.tg_token, self.tg_chat, self.tg_keys,
                                          "FLAT", sym, price, 0),
                                    kwargs={"ntfy_topic": st.ntfy_topic},
                                    daemon=True,
                                ).start()
                    except Exception:
                        continue
            try:
                conn.on("GatewayUserPosition", on_position_ws)
            except Exception:
                pass
            try:
                conn.on("PositionUpdate", on_position_ws)
            except Exception:
                pass
        except Exception as e:
            print(f"[WARN] Could not register GatewayLogout handler: {e}")

    async def _auto_reconnect(self):
        from project_x_py import TradingSuite
        self.reconnecting = True
        attempt_start = time.time()
        self.last_reconnect_time = attempt_start
        now = datetime.now(ET).strftime("%H:%M:%S")
        symbols = self._symbols_list()
        print(f"[{now}] [RECONNECT] Auto-reconnecting (attempt #{self.reconnect_failures + 1}, indicators preserved)...")
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
                st.last_known_price = None
                st.last_price_change_time = time.time()
                st.last_new_bar_time = time.time()

            self._register_gateway_logout()
            self.last_price_time = time.time()
            self.connection_alive = True
            self.disconnect_alert_sent = False

            self.reconnect_failures = 0
            self.reconnect_cooldown = self.RECONNECT_COOLDOWN_OK

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
            self.reconnect_failures += 1
            self.reconnect_cooldown = min(
                5 * (2 ** (self.reconnect_failures - 1)),
                self.RECONNECT_COOLDOWN_MAX,
            )
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] Failed (#{self.reconnect_failures}): {e} - retry in {self.reconnect_cooldown}s")
            self.suite = None
            for st in self.states.values():
                st.ctx = None
        finally:
            self.reconnecting = False

    async def _tick(self):
        if self.suite is None:
            if in_session() and not self.reconnecting:
                if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
                    await self._auto_reconnect()
            return

        now_ts = time.time()
        any_price_ok = False
        any_price_frozen = False
        frozen_sym = None
        for sym, st in self.states.items():
            if st.ctx is None:
                continue
            try:
                p = await st.ctx.data.get_current_price()
            except Exception:
                p = None
            if p is not None:
                any_price_ok = True
                if st.last_known_price is None or p != st.last_known_price:
                    st.last_known_price = p
                    st.last_price_change_time = now_ts
                if st.is_price_frozen(threshold=90):
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

        if any_price_frozen and in_session() and not self.reconnecting:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [FROZEN] {frozen_sym} price unchanged for 90+ seconds - forcing reconnect")
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
        connection_error = False
        for st in self.states.values():
            try:
                await st.tick()
            except Exception as e:
                print(f"[WARN] {st.symbol} tick() raised: {e}")
                connection_error = True
            if st.last_order_error in ("timeout", "error"):
                connection_error = True
                st.last_order_error = None

        if connection_error and not self.reconnecting:
            if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
                await self._auto_reconnect()

        if not self.reconnecting:
            for sym, st in self.states.items():
                stale_threshold = max(240, st.tick_interval * 4)
                if st.is_data_stale(stale_threshold):
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [STALE] {sym} no new brick for {stale_threshold}s - reconnecting")
                    self._notify_status(f"STATUS|{sym} data stale, reconnecting ({now} ET)")
                    if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
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
            if time.time() - self.last_reconnect_time > self.reconnect_cooldown:
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
