"""
TopstepX FCO Bot (LIVE)
=======================
Multi-symbol support: runs multiple instruments on one connection.

Strategy: Fancy Chande Oscillator (FCO) crossover
- FCO = dual-period Forecast Oscillator on hlc3, blended 50/50
  FCO(n) = ((hlc3 - LinReg(hlc3, n)) / hlc3) * 100
  Blended: 0.5 * FCO(14) + 0.5 * FCO(20), smoothed SMA(3)
- Signal line: WMA(6)
- LONG entry: FCO crosses UP through -1 (recovering from oversold)
- SHORT entry: FCO crosses DOWN through +1 (reversing from overbought)
- ADX(14) filter: only trade when ADX > threshold (optional)
- EXIT: opposite FCO signal flips position.
- Scale-in up to MAX_CONTRACTS on repeated signals.
- Bot-side TP available (incrementing dollar target).

Usage:
    python fco_bot.py --symbols "NQ:1:disabled" --candle-size 300 \\
                      --tg-token TOKEN --tg-chat CHAT
"""

import asyncio
import argparse
import gc
import signal
import json
import os
import time
import threading
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
        payload = {"ticker": symbol, "action": action, "price": price, "quantity": qty}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data,
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print(f"[TP-WEBHOOK] Sent {action} {symbol} @ {price:.2f}")
    except Exception as e:
        print(f"[TP-WEBHOOK] Failed: {e}")


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

SESSION_START = dtime(19, 0, 0)
SESSION_END = dtime(15, 30, 0)
TRADING_DAYS = [0, 1, 2, 3, 4, 6]
BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

# FCO default parameters
FCO_LENGTH_1 = 14
FCO_LENGTH_2 = 20
FCO_WEIGHT = 0.5
FCO_SMOOTH_LENGTH = 3
FCO_SIGNAL_LENGTH = 6
FCO_LONG_CROSS = -1.0
FCO_SHORT_CROSS = 1.0

# ADX filter
ADX_LENGTH = 14
ADX_SMOOTHING = 14
ADX_THRESHOLD = 20.0

POINT_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0,
    "YM": 5.0, "RTY": 10.0,
}


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
    return BLACKOUT_START <= datetime.now(ET).time() < BLACKOUT_END


# ============================================================
# Indicator Math
# ============================================================

def linreg_value(values, length):
    """Linear regression value at the most recent bar."""
    if len(values) < length:
        return None
    y = values[-length:]
    n = length
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    num = 0.0
    den = 0.0
    for i in range(n):
        dx = i - x_mean
        num += dx * (y[i] - y_mean)
        den += dx * dx
    if abs(den) < 1e-10:
        return y_mean
    slope = num / den
    return y_mean + slope * x_mean


def forecast_osc(values, length):
    """Chande Forecast Oscillator: ((current - linreg) / current) * 100."""
    lr = linreg_value(values, length)
    if lr is None:
        return None
    current = values[-1]
    if abs(current) < 1e-10:
        return 0.0
    return ((current - lr) / current) * 100.0


# ============================================================
# Per-Symbol Strategy State
# ============================================================

class SymbolState:
    def __init__(self, symbol, qty, ntfy_topic, tg_token, tg_chat, tg_keys,
                 candle_size=300, use_adx_filter=True, adx_threshold=20.0,
                 fco_long_cross=-1.0, fco_short_cross=1.0, tp_webhooks=None):
        self.symbol = symbol
        self.qty = qty
        self.candle_size = candle_size
        self.ntfy_topic = ntfy_topic
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys
        self.point_value = POINT_VALUES.get(symbol, 20.0)
        self.use_adx_filter = use_adx_filter
        self.adx_threshold = adx_threshold
        self.fco_long_cross = fco_long_cross
        self.fco_short_cross = fco_short_cross
        self.tp_webhooks = tp_webhooks or []

        # Candle building (time-based OHLC)
        self._candle_start_time = 0.0
        self._candle_open = None
        self._candle_high = None
        self._candle_low = None
        self._candle_close = None

        # Candle history
        self.candle_highs = []
        self.candle_lows = []
        self.candle_closes = []
        self.candle_hlc3 = []
        self.candle_count = 0

        # FCO state
        self.fco_raw_values = []
        self.fco_values = []
        self.fco_signal_values = []
        self.fco_value = None
        self.prev_fco_value = None
        self.fco_signal = None
        self.fco_momentum = None

        # ADX state
        self.adx_value = None
        self.plus_di = None
        self.minus_di = None
        self._smoothed_tr = None
        self._smoothed_plus_dm = None
        self._smoothed_minus_dm = None
        self._adx_dx_values = []

        # Position state
        self.position = 0
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.live_pnl = 0.0
        self.MAX_CONTRACTS = 5
        self.TP_BASE_DOLLARS = 100.0
        self.TP_INCREMENT_DOLLARS = 50.0

        # Connection tracking
        self.last_known_price = None
        self.last_price_change_time = None
        self.last_candle_time = None
        self.last_price = 0.0
        self.last_order_error = None

        # Position sync
        self.platform_flat_streak = 0
        self.last_position_poll_time = 0
        self.POSITION_POLL_INTERVAL = 30
        self.PLATFORM_FLAT_THRESHOLD = 5

        self.ctx = None
        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"trade_log_{symbol}.jsonl"
        )

    # ----------------------------------------------------------------
    # State persistence
    # ----------------------------------------------------------------

    def save_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "schema": 1,
            "saved_at": time.time(),
            "candle_highs": self.candle_highs[-300:],
            "candle_lows": self.candle_lows[-300:],
            "candle_closes": self.candle_closes[-300:],
            "candle_hlc3": self.candle_hlc3[-300:],
            "candle_count": self.candle_count,
            "fco_raw_values": self.fco_raw_values[-300:],
            "fco_values": self.fco_values[-300:],
            "fco_signal_values": self.fco_signal_values[-300:],
            "fco_value": self.fco_value,
            "prev_fco_value": self.prev_fco_value,
            "fco_signal": self.fco_signal,
            "fco_momentum": self.fco_momentum,
            "adx_value": self.adx_value,
            "_smoothed_tr": self._smoothed_tr,
            "_smoothed_plus_dm": self._smoothed_plus_dm,
            "_smoothed_minus_dm": self._smoothed_minus_dm,
            "_adx_dx_values": self._adx_dx_values[-100:],
            "position": self.position,
            "contracts_held": self.contracts_held,
            "entry_price": self.entry_price,
            "live_pnl": self.live_pnl,
            "last_price": self.last_price,
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        self.candle_highs = state.get("candle_highs", [])
        self.candle_lows = state.get("candle_lows", [])
        self.candle_closes = state.get("candle_closes", [])
        self.candle_hlc3 = state.get("candle_hlc3", [])
        self.candle_count = state.get("candle_count", 0)
        self.fco_raw_values = state.get("fco_raw_values", [])
        self.fco_values = state.get("fco_values", [])
        self.fco_signal_values = state.get("fco_signal_values", [])
        self.fco_value = state.get("fco_value")
        self.prev_fco_value = state.get("prev_fco_value")
        self.fco_signal = state.get("fco_signal")
        self.fco_momentum = state.get("fco_momentum")
        self.adx_value = state.get("adx_value")
        self._smoothed_tr = state.get("_smoothed_tr")
        self._smoothed_plus_dm = state.get("_smoothed_plus_dm")
        self._smoothed_minus_dm = state.get("_smoothed_minus_dm")
        self._adx_dx_values = state.get("_adx_dx_values", [])
        self.last_price = state.get("last_price", 0.0)

        position_age = time.time() - state.get("saved_at", 0)
        if position_age > position_ttl:
            print(f"  [{self.symbol}] Position state too old ({int(position_age)}s) - will sync from platform")
            self.position = 0
            self.contracts_held = 0
            self.entry_price = 0.0
        else:
            self.position = state.get("position", 0)
            self.contracts_held = state.get("contracts_held", 0)
            self.entry_price = state.get("entry_price", 0.0)
        self.live_pnl = state.get("live_pnl", 0.0)
        return True

    # ----------------------------------------------------------------
    # FCO computation
    # ----------------------------------------------------------------

    _MAX_HIST = 350

    def _compute_fco(self):
        """Compute FCO from hlc3 history after a candle closes."""
        fo1 = forecast_osc(self.candle_hlc3, FCO_LENGTH_1)
        fo2 = forecast_osc(self.candle_hlc3, FCO_LENGTH_2)
        if fo1 is None or fo2 is None:
            return

        raw = FCO_WEIGHT * fo1 + (1 - FCO_WEIGHT) * fo2
        self.fco_raw_values.append(raw)
        if len(self.fco_raw_values) > self._MAX_HIST:
            self.fco_raw_values = self.fco_raw_values[-self._MAX_HIST:]

        # SMA smoothing
        if len(self.fco_raw_values) >= FCO_SMOOTH_LENGTH:
            smoothed = sum(self.fco_raw_values[-FCO_SMOOTH_LENGTH:]) / FCO_SMOOTH_LENGTH
        else:
            smoothed = raw

        self.prev_fco_value = self.fco_value
        self.fco_value = smoothed
        self.fco_values.append(smoothed)
        if len(self.fco_values) > self._MAX_HIST:
            self.fco_values = self.fco_values[-self._MAX_HIST:]

        # WMA signal line
        if len(self.fco_values) >= FCO_SIGNAL_LENGTH:
            weights = list(range(1, FCO_SIGNAL_LENGTH + 1))
            total_w = sum(weights)
            recent = self.fco_values[-FCO_SIGNAL_LENGTH:]
            self.fco_signal = sum(v * w for v, w in zip(recent, weights)) / total_w
        else:
            self.fco_signal = smoothed

        self.fco_signal_values.append(self.fco_signal)
        if len(self.fco_signal_values) > self._MAX_HIST:
            self.fco_signal_values = self.fco_signal_values[-self._MAX_HIST:]

        self.fco_momentum = smoothed - self.fco_signal

    def _compute_adx(self):
        """Compute ADX from OHLC candle history (Wilder's method)."""
        n = len(self.candle_highs)
        if n < 2:
            return

        h = self.candle_highs[-1]
        l = self.candle_lows[-1]
        prev_h = self.candle_highs[-2]
        prev_l = self.candle_lows[-2]
        prev_c = self.candle_closes[-2]

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        if self._smoothed_tr is None:
            if n < ADX_LENGTH + 1:
                return
            init_tr = 0.0
            init_p = 0.0
            init_m = 0.0
            for i in range(1, ADX_LENGTH + 1):
                idx = n - ADX_LENGTH - 1 + i
                hi = self.candle_highs[idx]
                lo = self.candle_lows[idx]
                phi = self.candle_highs[idx - 1]
                plo = self.candle_lows[idx - 1]
                pcl = self.candle_closes[idx - 1]
                init_tr += max(hi - lo, abs(hi - pcl), abs(lo - pcl))
                um = hi - phi
                dm = plo - lo
                if um > dm and um > 0:
                    init_p += um
                if dm > um and dm > 0:
                    init_m += dm
            self._smoothed_tr = init_tr
            self._smoothed_plus_dm = init_p
            self._smoothed_minus_dm = init_m
        else:
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / ADX_LENGTH) + tr
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / ADX_LENGTH) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / ADX_LENGTH) + minus_dm

        if self._smoothed_tr > 0:
            self.plus_di = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
            self.minus_di = 100.0 * self._smoothed_minus_dm / self._smoothed_tr
        else:
            self.plus_di = 0.0
            self.minus_di = 0.0

        di_sum = self.plus_di + self.minus_di
        dx = 100.0 * abs(self.plus_di - self.minus_di) / di_sum if di_sum > 0 else 0.0
        self._adx_dx_values.append(dx)
        if len(self._adx_dx_values) > 100:
            self._adx_dx_values = self._adx_dx_values[-100:]

        if self.adx_value is None:
            if len(self._adx_dx_values) >= ADX_SMOOTHING:
                self.adx_value = sum(self._adx_dx_values[-ADX_SMOOTHING:]) / ADX_SMOOTHING
        else:
            self.adx_value = ((self.adx_value * (ADX_SMOOTHING - 1)) + dx) / ADX_SMOOTHING

    # ----------------------------------------------------------------
    # Candle building
    # ----------------------------------------------------------------

    def _update_candle(self, price: float, now_ts: float) -> bool:
        """Build time-based candles. Returns True when a candle closes."""
        if self._candle_open is None:
            self._candle_start_time = now_ts
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_close = price
            return False

        self._candle_high = max(self._candle_high, price)
        self._candle_low = min(self._candle_low, price)
        self._candle_close = price

        if now_ts - self._candle_start_time >= self.candle_size:
            h = self._candle_high
            l = self._candle_low
            c = self._candle_close
            hlc3 = (h + l + c) / 3.0

            self.candle_highs.append(h)
            self.candle_lows.append(l)
            self.candle_closes.append(c)
            self.candle_hlc3.append(hlc3)
            self.candle_count += 1

            if len(self.candle_hlc3) > self._MAX_HIST:
                self.candle_highs = self.candle_highs[-self._MAX_HIST:]
                self.candle_lows = self.candle_lows[-self._MAX_HIST:]
                self.candle_closes = self.candle_closes[-self._MAX_HIST:]
                self.candle_hlc3 = self.candle_hlc3[-self._MAX_HIST:]

            self._compute_fco()
            if self.use_adx_filter:
                self._compute_adx()

            self._candle_start_time = now_ts
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_close = price
            return True

        return False

    # ----------------------------------------------------------------
    # Signal detection
    # ----------------------------------------------------------------

    def check_signals(self) -> str:
        """Returns 'LONG', 'SHORT', or None on FCO crossover."""
        if self.fco_value is None or self.prev_fco_value is None:
            return None

        if self.use_adx_filter and self.adx_value is not None:
            if self.adx_value < self.adx_threshold:
                return None

        # LONG: FCO crosses UP through long_cross level (default -1)
        if self.prev_fco_value <= self.fco_long_cross and self.fco_value > self.fco_long_cross:
            return "LONG"

        # SHORT: FCO crosses DOWN through short_cross level (default +1)
        if self.prev_fco_value >= self.fco_short_cross and self.fco_value < self.fco_short_cross:
            return "SHORT"

        return None

    # ----------------------------------------------------------------
    # History seeding
    # ----------------------------------------------------------------

    async def seed_history(self):
        """Fill FCO/ADX buffers from TopstepX historical bars."""
        needed_candles = max(FCO_LENGTH_1, FCO_LENGTH_2) + FCO_SMOOTH_LENGTH + FCO_SIGNAL_LENGTH + ADX_LENGTH + 20
        needed_bars = min(needed_candles * self.candle_size, 50000)

        try:
            data = await self.ctx.data.get_data("1sec", bars=needed_bars)
        except Exception as e:
            print(f"[{self.symbol}] Historical fetch failed: {e}")
            return
        if data is None or len(data) == 0:
            print(f"[{self.symbol}] No historical data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[{self.symbol}] Seeding FCO from {len(rows)} 1sec bars...")

        self.candle_highs.clear()
        self.candle_lows.clear()
        self.candle_closes.clear()
        self.candle_hlc3.clear()
        self.fco_raw_values.clear()
        self.fco_values.clear()
        self.fco_signal_values.clear()
        self.fco_value = None
        self.prev_fco_value = None
        self.fco_signal = None
        self.fco_momentum = None
        self.adx_value = None
        self._smoothed_tr = None
        self._smoothed_plus_dm = None
        self._smoothed_minus_dm = None
        self._adx_dx_values.clear()
        self.candle_count = 0

        c_high = c_low = c_close = None
        bar_count = 0

        for row in rows:
            price = float(row["close"])
            h = float(row.get("high", price))
            l = float(row.get("low", price))

            if c_high is None:
                c_high = h
                c_low = l
                c_close = price
                bar_count = 1
            else:
                c_high = max(c_high, h)
                c_low = min(c_low, l)
                c_close = price
                bar_count += 1

            if bar_count >= self.candle_size:
                hlc3 = (c_high + c_low + c_close) / 3.0
                self.candle_highs.append(c_high)
                self.candle_lows.append(c_low)
                self.candle_closes.append(c_close)
                self.candle_hlc3.append(hlc3)
                self.candle_count += 1
                self._compute_fco()
                if self.use_adx_filter:
                    self._compute_adx()
                c_high = c_low = c_close = None
                bar_count = 0

        fco_str = f"{self.fco_value:.4f}" if self.fco_value is not None else "N/A"
        sig_str = f"{self.fco_signal:.4f}" if self.fco_signal is not None else "N/A"
        adx_str = f"{self.adx_value:.2f}" if self.adx_value is not None else "N/A"
        print(f"  [{self.symbol}] Seeded: {self.candle_count} candles | "
              f"FCO: {fco_str} | Signal: {sig_str} | ADX: {adx_str}")

    # ----------------------------------------------------------------
    # Status display
    # ----------------------------------------------------------------

    def print_status(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"
        fco_str = f"{self.fco_value:.4f}" if self.fco_value is not None else "N/A"
        sig_str = f"{self.fco_signal:.4f}" if self.fco_signal is not None else "N/A"
        mom_str = f"{self.fco_momentum:.4f}" if self.fco_momentum is not None else "N/A"
        adx_str = f"{self.adx_value:.2f}" if self.adx_value is not None else "N/A"
        tp_str = ""
        if self.contracts_held > 0:
            tp = self.TP_BASE_DOLLARS + self.TP_INCREMENT_DOLLARS * (self.contracts_held - 1)
            tp_str = f" | TP: ${tp:.0f}"

        print(f"  [{self.symbol} @ {now}]")
        print(f"    FCO: {fco_str} | Signal: {sig_str} | Momentum: {mom_str}")
        print(f"    ADX: {adx_str} | Candles: {self.candle_count} | "
              f"Candle size: {self.candle_size}s")
        print(f"    Levels: LONG cross-up {self.fco_long_cross} | "
              f"SHORT cross-down {self.fco_short_cross}")
        print(f"    Position: {pos_str} x{self.contracts_held} | "
              f"P&L: ${self.live_pnl:.2f} | PV=${self.point_value}/pt{tp_str}")

    def is_price_frozen(self, threshold: int = 180) -> bool:
        if self.last_price_change_time is None:
            return False
        return (time.time() - self.last_price_change_time) > threshold

    # ----------------------------------------------------------------
    # Tick loop (called by FCOBot every ~0.5s)
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

        if self.last_known_price is None or price != self.last_known_price:
            self.last_known_price = price
            self.last_price_change_time = time.time()
        self.last_price = price

        now_ts = time.time()
        now = datetime.now(ET).strftime("%H:%M:%S")

        new_candle = self._update_candle(price, now_ts)

        if new_candle:
            self.last_candle_time = now_ts
            fco_str = f"{self.fco_value:.4f}" if self.fco_value is not None else "N/A"
            sig_str = f"{self.fco_signal:.4f}" if self.fco_signal is not None else "N/A"
            adx_str = f"{self.adx_value:.2f}" if self.adx_value is not None else "N/A"
            print(f"[{now}] [{self.symbol} CANDLE #{self.candle_count}] "
                  f"H={self.candle_highs[-1]:.2f} L={self.candle_lows[-1]:.2f} "
                  f"C={self.candle_closes[-1]:.2f} | FCO: {fco_str} | "
                  f"Sig: {sig_str} | ADX: {adx_str}")

            direction = self.check_signals()
            if direction:
                print(f"[{now}] [{self.symbol} SIGNAL] {direction} | "
                      f"FCO: {self.prev_fco_value:.4f} -> {self.fco_value:.4f}")
                await self._handle_signal(direction, price, now)

        # Take profit check
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

    # ----------------------------------------------------------------
    # Signal handler
    # ----------------------------------------------------------------

    async def _handle_signal(self, direction: str, price: float, now: str):
        if self.position != 0:
            already_dir = "LONG" if self.position == 1 else "SHORT"
            if (direction == "LONG" and self.position == 1) or \
               (direction == "SHORT" and self.position == -1):
                if self.contracts_held >= self.MAX_CONTRACTS:
                    print(f"[{now}] [{self.symbol}] {direction} signal: "
                          f"already {already_dir} x{self.contracts_held} (max)")
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

            print(f"[{now}] [{self.symbol} FLIP] {already_dir} x{self.contracts_held} "
                  f"-> {direction}: closing first")
            await self._flatten(price, reason="SIGNAL_FLIP")
            if self.position != 0:
                print(f"[{now}] [{self.symbol}] flatten failed, aborting flip")
                return

        if direction == "LONG":
            await self._enter_long(price)
        else:
            await self._enter_short(price)

    # ----------------------------------------------------------------
    # Order placement (shielded against task cancellation)
    # ----------------------------------------------------------------

    async def _ensure_flat_before_entry(self):
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=4.0)
        except Exception:
            pass

    async def _cleanup_ghost_position(self):
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
        async def _do_order():
            try:
                response = await asyncio.wait_for(
                    self.ctx.orders.place_market_order(
                        contract_id=self.ctx.instrument_info.id,
                        side=side, size=self.qty),
                    timeout=15.0)
                if response.success:
                    self.contracts_held += 1
                    print(f"[{self.symbol}] Scale-in filled #{self.contracts_held}. "
                          f"ID: {response.orderId}")
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
        if self.position == 0:
            return True
        direction = "LONG" if self.position == 1 else "SHORT"
        saved_entry = self.entry_price
        saved_pos = self.position
        saved_qty = max(self.contracts_held, 1)
        trade_pnl = (price - saved_entry) * saved_pos * self.point_value * saved_qty
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] <<< EXITING {direction} x{saved_qty} @ {price:.2f} | "
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
                print(f"[{self.symbol}] close TIMEOUT — assuming closed externally")
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
            self._log_trade(direction, saved_entry, price, trade_pnl, reason, saved_entry_time)
        return close_ok

    def _log_trade(self, direction, entry_price, exit_price, pnl, reason, entry_time=None):
        now = datetime.now(ET)
        et_str = entry_time.strftime("%H:%M:%S") if entry_time else "N/A"
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
            "fco": self.fco_value,
            "fco_signal": self.fco_signal,
            "adx": self.adx_value,
            "account": os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown"),
            "session_pnl": self.live_pnl,
        }
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(trade) + "\n")
        except Exception as e:
            print(f"[{self.symbol}] Trade log write error: {e}")

    # ----------------------------------------------------------------
    # Position safety net (HTTP poll with divergence counter)
    # ----------------------------------------------------------------

    async def _query_platform_position(self):
        if self.ctx is None:
            return None
        try:
            positions = await asyncio.wait_for(
                self.ctx.positions.get_all_positions(), timeout=4.0)
        except Exception as e:
            print(f"[{self.symbol}] get_all_positions error: {e}")
            return None
        if positions is None or not isinstance(positions, (list, tuple)):
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
            return None
        return 0

    async def reconcile_position_with_platform(self):
        now_ts = time.time()
        if now_ts - self.last_position_poll_time < self.POSITION_POLL_INTERVAL:
            return
        self.last_position_poll_time = now_ts
        real_pos = await self._query_platform_position()
        if real_pos is None:
            return
        if self.position == 0 and real_pos == 0:
            self.platform_flat_streak = 0
            return
        if self.position != 0 and real_pos != 0:
            if (real_pos > 0) != (self.position > 0):
                print(f"[{self.symbol}] WARNING: bot={self.position}, platform={real_pos}")
                send_telegram(self.tg_token, self.tg_chat,
                              f"WARN|{self.symbol} state mismatch: bot={self.position} vs platform={real_pos}")
            self.platform_flat_streak = 0
            return
        if self.position != 0 and real_pos == 0:
            self.platform_flat_streak += 1
            print(f"[{self.symbol}] platform shows flat (streak {self.platform_flat_streak}"
                  f"/{self.PLATFORM_FLAT_THRESHOLD})")
            if self.platform_flat_streak >= self.PLATFORM_FLAT_THRESHOLD:
                direction = "LONG" if self.position == 1 else "SHORT"
                price = self.last_price or 0.0
                pnl_est = ((price - self.entry_price) * self.position
                           * self.point_value * max(self.contracts_held, 1)) if self.entry_price else 0.0
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"\n[{now}] [{self.symbol}] POSITION SYNC: platform closed "
                      f"{direction} externally. Est PnL: ${pnl_est:+.2f}")
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
            print(f"[{self.symbol}] WARN: bot=FLAT, platform shows {real_pos} contracts")
            self.platform_flat_streak = 0


# ============================================================
# Main Bot (connection, session, multi-symbol orchestration)
# ============================================================

class FCOBot:
    def __init__(self, symbol_configs, tg_token="", tg_chat="", tg_keys=None,
                 candle_size=300, use_adx_filter=True, adx_threshold=20.0,
                 fco_long_cross=-1.0, fco_short_cross=1.0, tp_webhooks=None):
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tp_webhooks = tp_webhooks or []

        self.states = {}
        for cfg in symbol_configs:
            sym = cfg["symbol"]
            state = SymbolState(
                symbol=sym,
                qty=cfg["qty"],
                ntfy_topic=cfg.get("ntfy_topic", ""),
                tg_token=tg_token,
                tg_chat=tg_chat,
                tg_keys=self.tg_keys,
                candle_size=candle_size,
                use_adx_filter=use_adx_filter,
                adx_threshold=adx_threshold,
                fco_long_cross=fco_long_cross,
                fco_short_cross=fco_short_cross,
                tp_webhooks=self.tp_webhooks,
            )
            self.states[sym] = state

        self.was_in_session = False
        self.last_price_time = None
        self.connection_alive = True
        self.disconnect_alert_sent = False
        self.STALE_THRESHOLD = 180
        self.RECONNECT_THRESHOLD = 300

        self.reconnecting = False
        self.last_reconnect_time = 0
        self.reconnect_failures = 0
        self.reconnect_cooldown = 5
        self.RECONNECT_COOLDOWN_OK = 30
        self.RECONNECT_COOLDOWN_MAX = 120

        self._ws_close_seen = {}
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
                        fco_s = f"{st.fco_value:.4f}" if st.fco_value is not None else "N/A"
                        adx_s = f"{st.adx_value:.2f}" if st.adx_value is not None else "N/A"
                        print(f"  [{sym}] Restored: candles={st.candle_count}, "
                              f"FCO={fco_s}, ADX={adx_s}")
                        restored_any = True
            return restored_any
        except Exception as e:
            print(f"[BOT] load_all_state error: {e}")
            return False

    def _notify_status(self, msg):
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
        candle_s = next(iter(self.states.values())).candle_size
        long_lvl = next(iter(self.states.values())).fco_long_cross
        short_lvl = next(iter(self.states.values())).fco_short_cross
        adx_on = next(iter(self.states.values())).use_adx_filter
        adx_thr = next(iter(self.states.values())).adx_threshold

        print(f"[BOT] FCO Strategy - LIVE MODE")
        print(f"[BOT] Symbols: {', '.join(symbols)}")
        for sym, st in self.states.items():
            print(f"[BOT]   {sym}: qty={st.qty}, pv=${st.point_value}/pt"
                  + (f", ntfy={st.ntfy_topic}" if st.ntfy_topic else ""))
        print(f"[BOT] FCO: Forecast Oscillator({FCO_LENGTH_1}/{FCO_LENGTH_2}) on hlc3, "
              f"SMA({FCO_SMOOTH_LENGTH}), Signal WMA({FCO_SIGNAL_LENGTH})")
        print(f"[BOT] ENTRY: LONG cross-up {long_lvl} | SHORT cross-down {short_lvl}")
        print(f"[BOT] Candle size: {candle_s}s ({candle_s // 60}min)")
        print(f"[BOT] ADX filter: {'ENABLED (>' + str(adx_thr) + ')' if adx_on else 'disabled'}")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - "
              f"{SESSION_END.strftime('%H:%M')} ET ({trading_day_str})")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        if self.tp_webhooks:
            print(f"[BOT] TradersPost webhooks: {len(self.tp_webhooks)} configured")
        print()

        self.suite = await TradingSuite.create(
            instruments=symbols,
            timeframes=["1sec"],
            initial_days=2,
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
                st.last_price = price
                print(f"[BOT] {sym} price: {price:.2f}")
            if not restored or not st.candle_hlc3:
                await st.seed_history()
            else:
                print(f"  [{sym}] Using restored indicator state (skipping seed)")

        for sym, st in self.states.items():
            if st.ctx is None:
                continue
            real_pos = await st._query_platform_position()
            if real_pos is None:
                print(f"  [{sym}] Could not query platform position on startup")
                continue
            if real_pos == 0 and st.position != 0:
                print(f"  [{sym}] Bot thought {st.position}, platform flat — resetting")
                send_telegram(self.tg_token, self.tg_chat,
                              f"STARTUP|{sym} bot thought in position, platform flat — reset")
                st.position = 0
                st.contracts_held = 0
                st.entry_price = 0.0
            elif real_pos != 0 and st.position == 0:
                print(f"  [{sym}] Bot thought flat, platform shows {real_pos} — closing")
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
        print(f"[BOT] Trading LIVE - FCO strategy ({', '.join(symbols)})")
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
                    print(f"[{now}] [WARN] Task cancelled - reconnecting...")
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
    # WebSocket handlers
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
            for arg in args:
                try:
                    cid = getattr(arg, "contract_id", None) or getattr(arg, "contractId", None)
                    size = float(getattr(arg, "size", 0) or getattr(arg, "net_pos", 0) or 0)
                    if cid is None or abs(size) >= 0.01:
                        continue
                    last_seen = self._ws_close_seen.get(cid, 0)
                    now_ts = time.time()
                    if now_ts - last_seen < 2.0:
                        continue
                    self._ws_close_seen[cid] = now_ts

                    for sym, st in self.states.items():
                        if st.ctx is None or st.ctx.instrument_info.id != cid:
                            continue
                        if st.position == 0:
                            continue
                        direction = "LONG" if st.position == 1 else "SHORT"
                        price = st.last_price or 0.0
                        pnl_est = ((price - st.entry_price) * st.position
                                   * st.point_value * max(st.contracts_held, 1)) if st.entry_price else 0.0
                        ts = datetime.now(ET).strftime("%H:%M:%S")
                        print(f"\n[{ts}] [{sym}] WS-SYNC: {direction} closed externally. "
                              f"Est PnL: ${pnl_est:+.2f}")

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

        registered = []
        try:
            conn.on("GatewayLogout", on_logout)
            registered.append("GatewayLogout")
        except Exception as e:
            print(f"[WARN] GatewayLogout register failed: {e}")
        try:
            conn.on("GatewayUserPosition", on_position_event)
            registered.append("GatewayUserPosition")
        except Exception as e:
            print(f"[WARN] GatewayUserPosition register failed: {e}")
        try:
            conn.on("PositionUpdate", on_position_event)
            registered.append("PositionUpdate")
        except Exception as e:
            print(f"[WARN] PositionUpdate register failed: {e}")
        if registered:
            print(f"[BOT] WS handlers registered: {', '.join(registered)}")

    # ----------------------------------------------------------------
    # Reconnect with exponential backoff
    # ----------------------------------------------------------------

    async def _auto_reconnect(self):
        from project_x_py import TradingSuite
        self.reconnecting = True
        self.last_reconnect_time = time.time()
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
                    timeframes=["1sec"],
                    initial_days=2,
                ),
                timeout=60.0,
            )
            for sym, st in self.states.items():
                st.ctx = self.suite[sym]
                st.last_known_price = None
                st.last_price_change_time = time.time()
                st.last_candle_time = time.time()
                st.platform_flat_streak = 0

            self._register_websocket_handlers()
            self.last_price_time = time.time()
            self.connection_alive = True
            self.disconnect_alert_sent = False
            self.reconnect_failures = 0
            self.reconnect_cooldown = self.RECONNECT_COOLDOWN_OK

            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] WebSocket restored")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|RECONNECTED ({now} ET)")

            for sym, st in self.states.items():
                if st.position != 0:
                    direction = "LONG" if st.position == 1 else "SHORT"
                    print(f"[{now}] [RECONNECT] {sym} still {direction} "
                          f"x{st.contracts_held} — keeping position")
                    send_telegram(self.tg_token, self.tg_chat,
                                  f"STATUS|{sym} still {direction} x{st.contracts_held} "
                                  f"after reconnect ({now} ET)")

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
                    print(f"[{now}] [ALERT] No price data for {int(elapsed)}s")
                    self._notify_status(f"STATUS|DISCONNECTED ({now} ET)")
                if elapsed > self.RECONNECT_THRESHOLD and not self.reconnecting:
                    if now_ts - self.last_reconnect_time > self.reconnect_cooldown:
                        await self._auto_reconnect()
            return

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
                    instruments=symbols, timeframes=["1sec"], initial_days=2)
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
        if in_blackout():
            return

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

        # Periodic state save
        if time.time() - self.last_state_save > 30:
            self.save_all_state()
            self.last_state_save = time.time()

        # GatewayLogout handling
        if self.last_gateway_logout_time > 0 and time.time() - self.last_gateway_logout_time > 5:
            self.last_gateway_logout_time = 0

        # Periodic GC + memory monitoring
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
                                print(f"[{now}] [MEM] RSS {rss_mb}MB exceeds 500MB — "
                                      f"saving state and restarting...")
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
                fco_str = f"{st.fco_value:.4f}" if st.fco_value is not None else "N/A"
                adx_str = f"{st.adx_value:.2f}" if st.adx_value is not None else "N/A"
                msg = (f"HEARTBEAT|{sym} alive ({now} ET) | {pos_str} | "
                       f"P&L: ${st.live_pnl:.2f} | FCO: {fco_str} | ADX: {adx_str}")
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
    """Parse 'NQ:1:ntfy-topic,ES:2' into list of config dicts."""
    configs = []
    for part in symbols_str.split(","):
        parts = part.strip().split(":")
        if len(parts) < 2:
            raise ValueError(f"Invalid symbol config '{part}'. "
                             f"Format: SYMBOL:QTY[:NTFY_TOPIC]")
        cfg = {
            "symbol": parts[0].strip().upper(),
            "qty": int(parts[1]),
            "ntfy_topic": parts[2].strip() if len(parts) > 2 else "",
        }
        configs.append(cfg)
    return configs


def main():
    parser = argparse.ArgumentParser(description="TopstepX FCO Bot (Multi-Symbol)")
    parser.add_argument("--symbols", default="NQ:1:disabled",
                        help="Multi-symbol config: 'NQ:1:ntfy,ES:2'")
    parser.add_argument("--candle-size", type=int, default=300,
                        help="Candle size in seconds (default 300 = 5min)")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--fco-long-cross", type=float, default=-1.0,
                        help="FCO level for LONG (cross UP through this, default -1)")
    parser.add_argument("--fco-short-cross", type=float, default=1.0,
                        help="FCO level for SHORT (cross DOWN through this, default +1)")
    parser.add_argument("--adx-threshold", type=float, default=20.0,
                        help="ADX threshold (only trade when ADX > this)")
    parser.add_argument("--no-adx", action="store_true",
                        help="Disable ADX filter")
    parser.add_argument("--tp-webhooks", default="",
                        help="Comma-separated TradersPost webhook URLs")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []
    tp_webhooks = [u.strip() for u in args.tp_webhooks.split(",") if u.strip()] if args.tp_webhooks else []

    symbol_configs = parse_symbol_configs(args.symbols)

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

    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 10_000_000:
            os.truncate(log_file, 0)
            print(f"[BOT] Log file truncated (>10MB)")
    except Exception:
        pass

    while not stopped:
        bot = FCOBot(
            symbol_configs=symbol_configs,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            candle_size=args.candle_size,
            use_adx_filter=not args.no_adx,
            adx_threshold=args.adx_threshold,
            fco_long_cross=args.fco_long_cross,
            fco_short_cross=args.fco_short_cross,
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
                              f"STATUS|FCO Bot crashed, restarting in {retry_delay}s ({now} ET)")
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
