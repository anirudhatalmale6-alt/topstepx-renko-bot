"""
TopstepX MMS (Market Maker Model) Bot
======================================
Strategy: ICT Market Maker Model with multi-timeframe analysis
- 15m candles: overall trend bias (HH/HL = bullish, LH/LL = bearish)
- 5m candles: structure (swing points, BOS detection, FVG zones)
- 1m candles: precise FVG entry timing
- Entry: price taps into FVG after BOS in trend direction
- SL: behind the liquidity sweep point (point 2)
- TP: nearest equal highs/lows (liquidity target)

Usage:
    python mms_bot.py --symbols "NQ:1" --tick-interval 1
"""

import asyncio
import argparse
import gc
import math
import signal as signal_mod
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
        msg = f"SIGNAL|{key}|{direction}|{symbol}|{price:.2f}|{qty}"
        send_telegram(token, chat_id, msg)
    if ntfy_topic:
        send_ntfy(ntfy_topic, f"{direction} {symbol} @ {price:.2f} x{qty}")
    if tp_webhooks:
        action = "buy" if direction == "LONG" else ("sell" if direction == "SHORT" else "exit")
        for wh in tp_webhooks:
            send_traderspost(wh, action, symbol, price, qty)


# ============================================================
# Session / Time Config
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(18, 0, 0)
SESSION_END = dtime(16, 0, 0)
TRADING_DAYS = [0, 1, 2, 3, 4, 6]

TRADE_SESSION_START = dtime(18, 0, 0)
TRADE_SESSION_END = dtime(16, 0, 0)

BLACKOUT_START = dtime(16, 10, 0)
BLACKOUT_END = dtime(16, 35, 0)

# ============================================================
# Strategy Constants
# ============================================================

STRUCTURE_TF = 300    # 5-minute candles for structure
ENTRY_TF = 60         # 1-minute candles for entry
TREND_TF = 900        # 15-minute candles for trend bias

SWING_LOOKBACK = 3    # candles on each side for swing detection
FVG_MIN_SIZE_PTS = 0.5  # minimum FVG gap size in points
EQH_TOLERANCE_PTS = 2.0  # how close two highs/lows need to be to count as "equal"
MAX_FVG_AGE = 20      # max candles old an FVG can be to still be valid
BOS_CONFIRM_CLOSE = True  # require candle CLOSE beyond swing for BOS

DAILY_LOSS_LIMIT = 1000.0
FEE_PER_CONTRACT = 4.60

MINTICK_VALUES = {
    "NQ": 0.25, "ES": 0.25, "MNQ": 0.25, "MES": 0.25,
    "YM": 1.0, "RTY": 0.10, "CL": 0.01, "GC": 0.10,
}
PV_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0,
    "YM": 5.0, "RTY": 50.0, "CL": 1000.0, "GC": 100.0,
}

WATCHDOG_TIMEOUT = 300
TICK_HEALTH_TIMEOUT = 90


def in_session() -> bool:
    now = datetime.now(ET)
    t = now.time()
    wd = now.weekday()
    if wd not in TRADING_DAYS:
        return False
    if SESSION_START > SESSION_END:
        return t >= SESSION_START or t < SESSION_END
    return SESSION_START <= t < SESSION_END


def in_blackout() -> bool:
    t = datetime.now(ET).time()
    return BLACKOUT_START <= t < BLACKOUT_END


def in_trade_session() -> bool:
    if in_blackout():
        return False
    t = datetime.now(ET).time()
    if TRADE_SESSION_START > TRADE_SESSION_END:
        return t >= TRADE_SESSION_START or t < TRADE_SESSION_END
    return TRADE_SESSION_START <= t < TRADE_SESSION_END


# ============================================================
# Candle Builder
# ============================================================

class CandleBuilder:
    def __init__(self, interval_secs: int):
        self.interval = interval_secs
        self.candles = []
        self._current = None
        self._max_candles = 500

    def feed(self, price: float, ts: float) -> dict:
        bucket = int(ts // self.interval) * self.interval
        if self._current is None or self._current["bucket"] != bucket:
            completed = self._current
            self._current = {
                "bucket": bucket, "open": price, "high": price,
                "low": price, "close": price, "volume": 1,
                "ts": bucket,
            }
            if completed and completed["volume"] > 0:
                self.candles.append(completed)
                if len(self.candles) > self._max_candles:
                    self.candles = self.candles[-self._max_candles:]
                return completed
        else:
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price
            self._current["volume"] += 1
        return None


# ============================================================
# Swing Point Detection
# ============================================================

class SwingPoint:
    def __init__(self, kind: str, price: float, index: int, candle_ts: float):
        self.kind = kind  # "high" or "low"
        self.price = price
        self.index = index
        self.ts = candle_ts

    def __repr__(self):
        return f"Swing({self.kind}={self.price:.2f}@{self.index})"


def detect_swings(candles: list, lookback: int = SWING_LOOKBACK) -> list:
    swings = []
    if len(candles) < lookback * 2 + 1:
        return swings
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        is_high = all(c["high"] >= candles[i - j]["high"] and
                      c["high"] >= candles[i + j]["high"]
                      for j in range(1, lookback + 1))
        if is_high:
            swings.append(SwingPoint("high", c["high"], i, c.get("ts", 0)))
        is_low = all(c["low"] <= candles[i - j]["low"] and
                     c["low"] <= candles[i + j]["low"]
                     for j in range(1, lookback + 1))
        if is_low:
            swings.append(SwingPoint("low", c["low"], i, c.get("ts", 0)))
    return swings


# ============================================================
# FVG (Fair Value Gap) Detection
# ============================================================

class FVG:
    def __init__(self, kind: str, top: float, bottom: float, index: int, candle_ts: float):
        self.kind = kind  # "bullish" or "bearish"
        self.top = top
        self.bottom = bottom
        self.mid = (top + bottom) / 2.0
        self.index = index
        self.ts = candle_ts
        self.filled = False

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def size(self) -> float:
        return self.top - self.bottom

    def __repr__(self):
        return f"FVG({self.kind} {self.bottom:.2f}-{self.top:.2f}@{self.index})"


def detect_fvgs(candles: list, min_size: float = FVG_MIN_SIZE_PTS) -> list:
    fvgs = []
    if len(candles) < 3:
        return fvgs
    for i in range(2, len(candles)):
        c0 = candles[i - 2]
        c2 = candles[i]
        # Bullish FVG: gap between c0.high and c2.low (price jumped up)
        if c2["low"] > c0["high"] and (c2["low"] - c0["high"]) >= min_size:
            fvgs.append(FVG("bullish", c2["low"], c0["high"], i, c2.get("ts", 0)))
        # Bearish FVG: gap between c0.low and c2.high (price dropped)
        if c0["low"] > c2["high"] and (c0["low"] - c2["high"]) >= min_size:
            fvgs.append(FVG("bearish", c0["low"], c2["high"], i, c2.get("ts", 0)))
    return fvgs


# ============================================================
# Equal Highs/Lows Detection
# ============================================================

def find_equal_levels(swings: list, tolerance: float = EQH_TOLERANCE_PTS):
    eqh = []
    eql = []
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if abs(highs[i].price - highs[j].price) <= tolerance:
                level = (highs[i].price + highs[j].price) / 2.0
                eqh.append({"level": level, "swings": [highs[i], highs[j]]})
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if abs(lows[i].price - lows[j].price) <= tolerance:
                level = (lows[i].price + lows[j].price) / 2.0
                eql.append({"level": level, "swings": [lows[i], lows[j]]})
    return eqh, eql


# ============================================================
# MMS Structure Tracker
# ============================================================

class ElliottWave:
    """Stores a detected 5-wave pattern."""
    def __init__(self, direction: str, waves: list, fvgs_at_2: list, fvgs_at_4: list):
        self.direction = direction  # "bullish_impulse" (up) or "bearish_impulse" (down)
        self.waves = waves          # list of 5 SwingPoint objects [w1, w2, w3, w4, w5]
        self.fvgs_at_2 = fvgs_at_2  # FVGs near wave 2 level (entry zones)
        self.fvgs_at_4 = fvgs_at_4  # FVGs near wave 4 level (entry zones)
        self.completed = False       # True when wave 5 is confirmed and reversal starts
        self.used = False

    @property
    def wave5_price(self):
        return self.waves[4].price if len(self.waves) >= 5 else None

    @property
    def wave2_price(self):
        return self.waves[1].price if len(self.waves) >= 2 else None

    @property
    def wave4_price(self):
        return self.waves[3].price if len(self.waves) >= 4 else None

    def __repr__(self):
        pts = [f"w{i+1}={w.price:.1f}" for i, w in enumerate(self.waves)]
        return f"Elliott({self.direction} {' '.join(pts)})"


def detect_elliott_5wave(swings: list, candles: list, tolerance_pct: float = 0.002):
    """
    Detect 5-wave Elliott patterns from swing points.

    Bullish impulse (up): w1=low, w2=high, w3=low(higher), w4=high(higher), w5=low(highest low)
    Wait... Actually per Adz Trades pics:
    Bullish 5-wave: 1(start low) -> 2(first low) -> 3(higher high) -> 4(pullback low) -> 5(highest high)
    Then reversal down -> SHORT at FVG near w4/w2

    Bearish 5-wave: 1(start high) -> 2(first high) -> 3(lower low) -> 4(pullback high) -> 5(lowest low)
    Then reversal up -> LONG at FVG near w4/w2

    Looking at the images more carefully:
    IMG_5448 bullish: waves go 1(low)->2(lower low)->3(mid high)->4(pullback)->5(highest)
    Actually the standard Elliott:
    - Bullish: 1=impulse up, 2=corrective down, 3=impulse up, 4=corrective down, 5=impulse up
    So swings: start->w1(high)->w2(low)->w3(higher high)->w4(low)->w5(highest high)

    IMG_5446 bearish: (1)=impulse down, (2)=corrective up(top), (3)=lower low, (4)=corrective up, (5)=lowest low
    Swings: start->w1(low)->w2(high)->w3(lower low)->w4(high)->w5(lowest low)
    """
    patterns = []
    if len(swings) < 5:
        return patterns

    # Try all combinations of 5 consecutive swing alternations
    for start_i in range(len(swings) - 4):
        w = swings[start_i:start_i + 5]

        # Check alternation: H-L-H-L-H or L-H-L-H-L
        kinds = [s.kind for s in w]

        # Bullish impulse: H-L-H-L-H (highs getting higher, wave 3 > wave 1, wave 5 > wave 3)
        if kinds == ["high", "low", "high", "low", "high"]:
            w1, w2, w3, w4, w5 = w
            # Elliott rules for bullish impulse:
            # w3 > w1 (wave 3 higher than wave 1)
            # w5 > w3 (wave 5 higher than wave 3) - or at least w5 > w1
            # w4 > w2 (wave 4 low stays above wave 2 low) - no overlap rule
            # w2 doesn't retrace below the start
            if (w3.price > w1.price and w5.price > w1.price and
                    w4.price > w2.price):
                patterns.append(ElliottWave("bullish_impulse", list(w), [], []))

        # Bearish impulse: L-H-L-H-L (lows getting lower, wave 3 < wave 1, wave 5 < wave 3)
        elif kinds == ["low", "high", "low", "high", "low"]:
            w1, w2, w3, w4, w5 = w
            # Elliott rules for bearish impulse:
            # w3 < w1 (wave 3 lower than wave 1)
            # w5 < w1 (wave 5 lower)
            # w4 < w2 (wave 4 high stays below wave 2 high) - no overlap
            if (w3.price < w1.price and w5.price < w1.price and
                    w4.price < w2.price):
                patterns.append(ElliottWave("bearish_impulse", list(w), [], []))

    return patterns


class MMSState:
    """Tracks the 1-2-3-4-5 Market Maker Model pattern (Elliott Wave reversal)."""

    def __init__(self):
        self.trend_bias = None         # "bullish" or "bearish" from HTF
        self.swing_history = []        # recent swings from structure TF
        self.active_patterns = []      # list of ElliottWave patterns detected
        self.all_fvgs = []             # all FVGs on structure TF
        self.eqh_levels = []
        self.eql_levels = []
        self.last_structure_candle_count = 0
        self._reversal_confirmed = {}  # pattern id -> bool

    def update_trend(self, candles_15m: list):
        """Determine HTF trend from 15m candles using swing structure."""
        if len(candles_15m) < 20:
            return
        swings = detect_swings(candles_15m, lookback=2)
        if len(swings) < 4:
            return
        recent = swings[-4:]
        highs = [s for s in recent if s.kind == "high"]
        lows = [s for s in recent if s.kind == "low"]
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
                self.trend_bias = "bullish"
            elif highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
                self.trend_bias = "bearish"

    def update_structure(self, candles_5m: list):
        """Detect 5-wave patterns and FVG zones on 5m structure timeframe."""
        if len(candles_5m) < 15:
            return
        if len(candles_5m) == self.last_structure_candle_count:
            return
        self.last_structure_candle_count = len(candles_5m)

        swings = detect_swings(candles_5m, lookback=SWING_LOOKBACK)
        self.swing_history = swings[-30:] if swings else []

        # Detect all FVGs
        fvgs = detect_fvgs(candles_5m[-50:], FVG_MIN_SIZE_PTS)
        n_candles = len(candles_5m)
        self.all_fvgs = []
        for fvg in fvgs:
            age = 50 - fvg.index  # age in candles from end of window
            if age <= MAX_FVG_AGE:
                # Adjust FVG index to absolute position
                fvg.index = n_candles - 50 + fvg.index
                self.all_fvgs.append(fvg)

        # Detect EQH/EQL
        eqh, eql = find_equal_levels(self.swing_history, EQH_TOLERANCE_PTS)
        self.eqh_levels = [e["level"] for e in eqh]
        self.eql_levels = [e["level"] for e in eql]

        # Detect 5-wave Elliott patterns
        if len(self.swing_history) >= 5:
            patterns = detect_elliott_5wave(self.swing_history, candles_5m)
            # Only keep new patterns not already tracked
            existing_w5_prices = set()
            for p in self.active_patterns:
                if p.wave5_price is not None:
                    existing_w5_prices.add(round(p.wave5_price, 1))

            for pat in patterns:
                w5_key = round(pat.wave5_price, 1) if pat.wave5_price else 0
                if w5_key not in existing_w5_prices and not pat.used:
                    # Find FVGs near wave 2 and wave 4 levels
                    w2_price = pat.wave2_price
                    w4_price = pat.wave4_price
                    fvg_tolerance = 5.0  # points range to match FVG to wave level

                    for fvg in self.all_fvgs:
                        if pat.direction == "bullish_impulse":
                            # Wave 2 is a swing low, wave 4 is a swing low
                            # FVGs near those levels (bearish FVGs = gap down = price drops into)
                            if abs(fvg.mid - w2_price) <= fvg_tolerance:
                                pat.fvgs_at_2.append(fvg)
                            if abs(fvg.mid - w4_price) <= fvg_tolerance:
                                pat.fvgs_at_4.append(fvg)
                        else:
                            # Bearish impulse: wave 2 is swing high, wave 4 is swing high
                            if abs(fvg.mid - w2_price) <= fvg_tolerance:
                                pat.fvgs_at_2.append(fvg)
                            if abs(fvg.mid - w4_price) <= fvg_tolerance:
                                pat.fvgs_at_4.append(fvg)

                    self.active_patterns.append(pat)
                    existing_w5_prices.add(w5_key)
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [MMS 5-WAVE] {pat.direction.upper()} detected: "
                          f"w1={pat.waves[0].price:.1f} w2={pat.waves[1].price:.1f} "
                          f"w3={pat.waves[2].price:.1f} w4={pat.waves[3].price:.1f} "
                          f"w5={pat.waves[4].price:.1f} | "
                          f"FVGs@w2: {len(pat.fvgs_at_2)} FVGs@w4: {len(pat.fvgs_at_4)}")

            # Prune old patterns (keep last 5)
            self.active_patterns = [p for p in self.active_patterns if not p.used][-5:]

    def _check_reversal(self, pattern: ElliottWave, price: float) -> bool:
        """Check if price has reversed from wave 5 (confirming pattern completion)."""
        w5 = pattern.wave5_price
        if w5 is None:
            return False
        if pattern.direction == "bullish_impulse":
            # Bullish 5-wave topped at w5 (a high). Reversal = price drops below w5
            return price < w5
        else:
            # Bearish 5-wave bottomed at w5 (a low). Reversal = price rises above w5
            return price > w5

    def get_entry_signal(self, price: float) -> dict:
        """
        Check if price taps into FVG at wave 2 or wave 4 level after 5-wave completion.

        After bullish 5-wave completes -> price reverses DOWN -> SHORT at FVG near w4 (then w2)
        After bearish 5-wave completes -> price reverses UP -> LONG at FVG near w4 (then w2)
        """
        for pattern in self.active_patterns:
            if pattern.used:
                continue
            if not self._check_reversal(pattern, price):
                continue

            w2 = pattern.wave2_price
            w4 = pattern.wave4_price
            w5 = pattern.wave5_price

            if pattern.direction == "bullish_impulse":
                # After bullish 5-wave: trade the reversal SHORT
                # Entry: price rises back to FVG near wave 4 (preferred) or wave 2
                # SL: above wave 5 (the high)
                # TP: below wave 2 or EQL level

                # Check FVGs at wave 4 first (closer, preferred entry)
                for fvg in pattern.fvgs_at_4:
                    if fvg.contains(price) and not fvg.filled:
                        sl_price = w5 + 2.0  # above the wave 5 high
                        sl_pts = abs(sl_price - price)
                        # TP at wave 2 level or EQL
                        tp_price = None
                        for lvl in sorted(self.eql_levels, reverse=True):
                            if lvl < price - 2.0:
                                tp_price = lvl
                                break
                        if tp_price is None:
                            tp_price = w2  # target wave 2 level
                        tp_pts = abs(price - tp_price)
                        return {
                            "direction": "SHORT",
                            "fvg": fvg,
                            "sl_price": sl_price,
                            "sl_pts": sl_pts,
                            "tp_price": tp_price,
                            "tp_pts": tp_pts,
                            "wave_pattern": str(pattern),
                            "entry_wave": "w4",
                            "pattern": pattern,
                        }

                # Then check FVGs at wave 2
                for fvg in pattern.fvgs_at_2:
                    if fvg.contains(price) and not fvg.filled:
                        sl_price = w5 + 2.0
                        sl_pts = abs(sl_price - price)
                        tp_price = None
                        for lvl in sorted(self.eql_levels, reverse=True):
                            if lvl < price - 2.0:
                                tp_price = lvl
                                break
                        if tp_price is None:
                            tp_price = price - sl_pts * 2.0
                        tp_pts = abs(price - tp_price)
                        return {
                            "direction": "SHORT",
                            "fvg": fvg,
                            "sl_price": sl_price,
                            "sl_pts": sl_pts,
                            "tp_price": tp_price,
                            "tp_pts": tp_pts,
                            "wave_pattern": str(pattern),
                            "entry_wave": "w2",
                            "pattern": pattern,
                        }

            elif pattern.direction == "bearish_impulse":
                # After bearish 5-wave: trade the reversal LONG
                # Entry: price drops back to FVG near wave 4 (preferred) or wave 2
                # SL: below wave 5 (the low)
                # TP: above wave 2 or EQH level

                for fvg in pattern.fvgs_at_4:
                    if fvg.contains(price) and not fvg.filled:
                        sl_price = w5 - 2.0  # below wave 5 low
                        sl_pts = abs(price - sl_price)
                        tp_price = None
                        for lvl in sorted(self.eqh_levels):
                            if lvl > price + 2.0:
                                tp_price = lvl
                                break
                        if tp_price is None:
                            tp_price = w2  # target wave 2 level
                        tp_pts = abs(tp_price - price)
                        return {
                            "direction": "LONG",
                            "fvg": fvg,
                            "sl_price": sl_price,
                            "sl_pts": sl_pts,
                            "tp_price": tp_price,
                            "tp_pts": tp_pts,
                            "wave_pattern": str(pattern),
                            "entry_wave": "w4",
                            "pattern": pattern,
                        }

                for fvg in pattern.fvgs_at_2:
                    if fvg.contains(price) and not fvg.filled:
                        sl_price = w5 - 2.0
                        sl_pts = abs(price - sl_price)
                        tp_price = None
                        for lvl in sorted(self.eqh_levels):
                            if lvl > price + 2.0:
                                tp_price = lvl
                                break
                        if tp_price is None:
                            tp_price = price + sl_pts * 2.0
                        tp_pts = abs(tp_price - price)
                        return {
                            "direction": "LONG",
                            "fvg": fvg,
                            "sl_price": sl_price,
                            "sl_pts": sl_pts,
                            "tp_price": tp_price,
                            "tp_pts": tp_pts,
                            "wave_pattern": str(pattern),
                            "entry_wave": "w2",
                            "pattern": pattern,
                        }

        return None

    def invalidate_fvg(self, fvg: FVG):
        """Mark an FVG as filled/used."""
        fvg.filled = True

    def mark_pattern_used(self, pattern):
        """Mark a pattern as used after trade entry."""
        pattern.used = True
        self.active_patterns = [p for p in self.active_patterns if not p.used]

    def reset_after_trade(self):
        """Reset state after a trade so we wait for a fresh setup."""
        pass  # patterns auto-cleaned by mark_pattern_used


# ============================================================
# Per-Symbol Strategy State
# ============================================================

class SymbolState:
    def __init__(self, symbol: str, qty: int, ntfy_topic: str = "",
                 tg_token: str = "", tg_chat: str = "", tg_keys: list = None,
                 tp_webhooks: list = None):
        self.symbol = symbol
        self.base_qty = qty
        self.ntfy_topic = ntfy_topic
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.tp_webhooks = tp_webhooks or []
        self.pv = PV_VALUES.get(symbol, 20.0)

        # Multi-timeframe candle builders
        self.candles_1m = CandleBuilder(ENTRY_TF)
        self.candles_5m = CandleBuilder(STRUCTURE_TF)
        self.candles_15m = CandleBuilder(TREND_TF)

        # MMS strategy state
        self.mms = MMSState()

        self.last_price = 0.0
        self.last_tick_time = 0.0

        # Position tracking
        self.position = 0
        self.contracts_held = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.active_sl_price = 0.0
        self.active_tp_price = 0.0
        self.active_sl_pts = 0.0
        self.active_tp_pts = 0.0
        self.live_pnl = 0.0
        self.trade_mae = 0.0
        self.trade_mfe = 0.0
        self.daily_loss = 0.0
        self._tp_limit_order_id = None
        self._tp_limit_price = None

        self.ctx = None
        self._suite_client = None
        self._start_balance = None
        self._pnl_session_day = None
        self._trade_count = 0

    async def _sync_entry_price_from_platform(self) -> None:
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

    async def _sync_pnl_from_platform(self):
        if not self._suite_client:
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

    async def _enter_long(self, price: float, sl_price: float, tp_price: float,
                          sl_pts: float, tp_pts: float):
        if self.position != 0:
            return False
        await self._ensure_flat_before_entry()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")

        print(f"\n[{now}] [{self.symbol}] >>> MMS LONG x{qty} @ {price:.2f} | "
              f"SL={sl_price:.2f} ({sl_pts:.1f}pts) | TP={tp_price:.2f} ({tp_pts:.1f}pts) | "
              f"Session P&L: ${self.live_pnl:.2f}")

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
                self.active_sl_price = sl_price
                self.active_tp_price = tp_price
                self.active_sl_pts = sl_pts
                self.active_tp_pts = tp_pts
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._trade_count += 1
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

    async def _enter_short(self, price: float, sl_price: float, tp_price: float,
                           sl_pts: float, tp_pts: float):
        if self.position != 0:
            return False
        await self._ensure_flat_before_entry()
        qty = self.base_qty
        now = datetime.now(ET).strftime("%H:%M:%S")

        print(f"\n[{now}] [{self.symbol}] >>> MMS SHORT x{qty} @ {price:.2f} | "
              f"SL={sl_price:.2f} ({sl_pts:.1f}pts) | TP={tp_price:.2f} ({tp_pts:.1f}pts) | "
              f"Session P&L: ${self.live_pnl:.2f}")

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
                self.active_sl_price = sl_price
                self.active_tp_price = tp_price
                self.active_sl_pts = sl_pts
                self.active_tp_pts = tp_pts
                self.trade_mae = 0.0
                self.trade_mfe = 0.0
                self._trade_count += 1
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

    async def _flatten(self, price: float, reason: str):
        if self.position == 0:
            return
        now_str = datetime.now(ET).strftime("%H:%M:%S")
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl_before = self.live_pnl

        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=10.0)
        except Exception as e:
            print(f"[{self.symbol}] Flatten error: {e}")

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
        self._tp_limit_order_id = None
        self._tp_limit_price = None

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

        self.mms.reset_after_trade()

    def tick(self, price: float, ts: float = None):
        """Process a tick. MMS strategy."""
        if ts is None:
            ts = time.time()
        self.last_price = price
        self.last_tick_time = ts
        actions = []

        now_et = datetime.now(ET)
        session_day = now_et.date() if now_et.time() >= dtime(18, 0, 0) else now_et.date() - timedelta(days=1)
        if self._pnl_session_day is None or self._pnl_session_day != session_day:
            self._pnl_session_day = session_day
            self._start_balance = None
            self.live_pnl = 0.0
            self.daily_loss = 0.0
            self._trade_count = 0
            self.mms = MMSState()
            print(f"[{self.symbol} SESSION] New session day {session_day} — all reset")

        # Feed all three timeframes
        self.candles_1m.feed(price, ts)
        completed_5m = self.candles_5m.feed(price, ts)
        completed_15m = self.candles_15m.feed(price, ts)

        # Update trend on new 15m candle
        if completed_15m:
            self.mms.update_trend(self.candles_15m.candles)
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now_str}] [{self.symbol} 15M] Trend: {self.mms.trend_bias or 'neutral'} "
                  f"| candles: {len(self.candles_15m.candles)}")

        # Update structure on new 5m candle
        if completed_5m:
            self.mms.update_structure(self.candles_5m.candles)
            now_str = datetime.now(ET).strftime("%H:%M:%S")
            n_fvg = len(self.mms.all_fvgs)
            n_pat = len(self.mms.active_patterns)
            n_eqh = len(self.mms.eqh_levels)
            n_eql = len(self.mms.eql_levels)
            print(f"[{now_str}] [{self.symbol} 5M] Patterns: {n_pat} | "
                  f"FVGs: {n_fvg} | EQH: {n_eqh} | EQL: {n_eql}")

        # Position management
        if self.position != 0:
            if self.position == 1:
                unrealized = (price - self.entry_price) * self.pv * self.contracts_held
            else:
                unrealized = (self.entry_price - price) * self.pv * self.contracts_held
            self.trade_mfe = max(self.trade_mfe, unrealized)
            self.trade_mae = min(self.trade_mae, unrealized)

            # SL check
            if self.position == 1 and price <= self.active_sl_price:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} MMS-SL] LONG — price {price:.2f} "
                      f"<= SL {self.active_sl_price:.2f}")
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "MMS_STOP_LOSS"))
                return actions
            elif self.position == -1 and price >= self.active_sl_price:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [{self.symbol} MMS-SL] SHORT — price {price:.2f} "
                      f">= SL {self.active_sl_price:.2f}")
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "MMS_STOP_LOSS"))
                return actions

            # Hard backstop
            if unrealized <= -DAILY_LOSS_LIMIT:
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now_str}] [{self.symbol} MAX-LOSS] {direction} "
                      f"unrealized ${unrealized:.0f} — FORCE EXIT")
                if self._tp_limit_order_id:
                    actions.append(("cancel_tp_limit",))
                actions.append(("flatten", price, "MAX_LOSS"))
                return actions

            # TP check: place limit order
            if unrealized >= self.active_tp_pts * self.pv * self.contracts_held * 0.8:
                if not self._tp_limit_order_id:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    direction = "LONG" if self.position == 1 else "SHORT"
                    print(f"[{now_str}] [{self.symbol} MMS-TP] {direction} "
                          f"nearing TP @ {self.active_tp_price:.2f} — placing limit")
                    actions.append(("tp_limit", price))
                    return actions

        # Entry logic: check for FVG tap on 1m price
        if self.position == 0 and in_trade_session():
            if abs(self.daily_loss) >= DAILY_LOSS_LIMIT:
                return actions

            signal = self.mms.get_entry_signal(price)
            if signal:
                sl_pts = signal["sl_pts"]
                tp_pts = signal["tp_pts"]
                # Risk filter: skip if SL is too wide (> 50 pts on NQ = $1000)
                max_sl = DAILY_LOSS_LIMIT / (self.pv * self.base_qty)
                if sl_pts > max_sl:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} MMS-SKIP] SL {sl_pts:.1f}pts > "
                          f"max {max_sl:.1f}pts — too wide")
                    self.mms.invalidate_fvg(signal["fvg"])
                    return actions
                # Risk filter: skip if R:R < 1.5
                if tp_pts / max(sl_pts, 0.1) < 1.5:
                    now_str = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now_str}] [{self.symbol} MMS-SKIP] R:R {tp_pts/max(sl_pts,0.1):.1f} < 1.5")
                    self.mms.invalidate_fvg(signal["fvg"])
                    return actions

                now_str = datetime.now(ET).strftime("%H:%M:%S")
                wave_info = signal.get("wave_pattern", "")
                entry_w = signal.get("entry_wave", "")
                print(f"[{now_str}] [{self.symbol} MMS-SIGNAL] {signal['direction']} "
                      f"FVG tap @ {price:.2f} (entry at {entry_w}) | "
                      f"SL={signal['sl_price']:.2f} TP={signal['tp_price']:.2f} "
                      f"R:R={tp_pts/max(sl_pts,0.1):.1f} | {wave_info}")

                self.mms.invalidate_fvg(signal["fvg"])
                if "pattern" in signal:
                    self.mms.mark_pattern_used(signal["pattern"])

                if signal["direction"] == "LONG":
                    actions.append(("enter_long", price, signal["sl_price"],
                                    signal["tp_price"], sl_pts, tp_pts))
                else:
                    actions.append(("enter_short", price, signal["sl_price"],
                                    signal["tp_price"], sl_pts, tp_pts))

        return actions

    def save_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "saved_at": time.time(),
            "position": self.position,
            "contracts_held": self.contracts_held,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "active_sl_price": self.active_sl_price,
            "active_tp_price": self.active_tp_price,
            "active_sl_pts": self.active_sl_pts,
            "active_tp_pts": self.active_tp_pts,
            "live_pnl": self.live_pnl,
            "trade_mae": self.trade_mae,
            "trade_mfe": self.trade_mfe,
            "daily_loss": self.daily_loss,
            "trade_count": self._trade_count,
            "trend_bias": self.mms.trend_bias,
            "candle_1m_current": self.candles_1m._current,
            "candle_1m_history": self.candles_1m.candles[-50:],
            "candle_5m_current": self.candles_5m._current,
            "candle_5m_history": self.candles_5m.candles[-50:],
            "candle_15m_current": self.candles_15m._current,
            "candle_15m_history": self.candles_15m.candles[-50:],
        }

    def restore_state(self, state: dict, position_ttl: int = 600) -> bool:
        age = time.time() - state.get("saved_at", 0)
        self.daily_loss = state.get("daily_loss", 0.0)
        self._trade_count = state.get("trade_count", 0)

        # Restore candles
        self.candles_1m._current = state.get("candle_1m_current")
        self.candles_1m.candles = state.get("candle_1m_history", [])
        self.candles_5m._current = state.get("candle_5m_current")
        self.candles_5m.candles = state.get("candle_5m_history", [])
        self.candles_15m._current = state.get("candle_15m_current")
        self.candles_15m.candles = state.get("candle_15m_history", [])

        # Restore MMS bias
        self.mms.trend_bias = state.get("trend_bias")

        pos = state.get("position", 0)
        if pos != 0 and age < position_ttl:
            self.position = pos
            self.contracts_held = state.get("contracts_held", 0)
            self.entry_price = state.get("entry_price", 0.0)
            self.entry_time = state.get("entry_time", 0.0)
            self.active_sl_price = state.get("active_sl_price", 0.0)
            self.active_tp_price = state.get("active_tp_price", 0.0)
            self.active_sl_pts = state.get("active_sl_pts", 0.0)
            self.active_tp_pts = state.get("active_tp_pts", 0.0)
            self.live_pnl = state.get("live_pnl", 0.0)
            self.trade_mae = state.get("trade_mae", 0.0)
            self.trade_mfe = state.get("trade_mfe", 0.0)
            print(f"  [{self.symbol}] Restored: pos={self.position}, "
                  f"entry={self.entry_price:.2f}, "
                  f"SL={self.active_sl_price:.2f}, TP={self.active_tp_price:.2f}")
            return True
        elif pos != 0:
            print(f"  [{self.symbol}] State too old ({age:.0f}s) — starting flat")
        return False


# ============================================================
# Bot Runner (TopstepX Connection Management)
# ============================================================

class MMSBot:
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
        self.state_file = os.path.join(os.getcwd(), "bot_state_mms.json")
        self.last_tick_time = time.time()
        self.last_real_tick_time = time.time()
        self._reconnect_count = 0
        self._max_reconnects = 5

        for cfg in symbol_configs:
            sym = cfg["symbol"]
            self.states[sym] = SymbolState(
                symbol=sym, qty=cfg["qty"],
                ntfy_topic=cfg.get("ntfy_topic", ""),
                tg_token=tg_token, tg_chat=tg_chat,
                tg_keys=self.tg_keys, tp_webhooks=self.tp_webhooks)

    def _symbols_list(self):
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

    def load_all_state(self):
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

                        st.mms.reset_after_trade()
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
        print(f"[BOT] MMS Bot starting...")
        print(f"[BOT] Strategy: Elliott 5-Wave Reversal + FVG entry at w2/w4")
        print(f"[BOT] Structure: 5m | Entry: 1m | Trend: 15m")
        print(f"[BOT] Entry: FVG tap at wave 2/4 after 5-wave reversal")
        print(f"[BOT] SL: beyond wave 5 (impulse extreme)")
        print(f"[BOT] TP: EQH/EQL levels or wave 2 target")
        print(f"[BOT] Min R:R = 1.5 | Max SL = ${DAILY_LOSS_LIMIT:.0f}")
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
        print(f"[BOT] Trading LIVE — MMS strategy ({', '.join(symbols)})")
        print(f"[BOT] Watchdog: {WATCHDOG_TIMEOUT}s timeout")
        print(f"[BOT] Press Ctrl+C to stop")

        for sym, st in self.states.items():
            msg = (f"STATUS|MMS Bot started\n"
                   f"Account: {acct}\n"
                   f"Strategy: ICT MMS (BOS + FVG)\n"
                   f"Timeframes: 15m/5m/1m\n"
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
                        await st._enter_long(action[1], action[2], action[3],
                                             action[4], action[5])
                    elif action[0] == "enter_short":
                        await st._enter_short(action[1], action[2], action[3],
                                              action[4], action[5])
                    elif action[0] == "cancel_tp_limit":
                        if st._tp_limit_order_id:
                            try:
                                await asyncio.wait_for(
                                    st.ctx.orders.cancel_order(st._tp_limit_order_id),
                                    timeout=5.0)
                                print(f"[{sym}] TP limit order cancelled")
                            except Exception as e:
                                print(f"[{sym}] TP limit cancel failed: {e}")
                            st._tp_limit_order_id = None
                            st._tp_limit_price = None
                    elif action[0] == "tp_limit":
                        now_str = datetime.now(ET).strftime("%H:%M:%S")
                        try:
                            positions = await asyncio.wait_for(
                                st._suite_client.search_open_positions(), timeout=8.0)
                            contract_id = st.ctx.instrument_info.id
                            real_avg = None
                            real_size = 0
                            for p in positions:
                                if p.contractId == contract_id and p.size > 0:
                                    real_avg = p.averagePrice
                                    real_size = p.size
                                    break
                            if real_avg and real_size > 0:
                                fees = FEE_PER_CONTRACT * real_size
                                tick_size = MINTICK_VALUES.get(sym, 0.25)
                                if st.position == 1:
                                    limit_price = st.active_tp_price
                                    limit_price = math.ceil(limit_price / tick_size) * tick_size
                                    side = 1
                                else:
                                    limit_price = st.active_tp_price
                                    limit_price = math.floor(limit_price / tick_size) * tick_size
                                    side = 0
                                response = await asyncio.wait_for(
                                    st.ctx.orders.place_limit_order(
                                        contract_id=contract_id, side=side,
                                        size=real_size, limit_price=limit_price),
                                    timeout=10.0)
                                if response.success:
                                    st._tp_limit_order_id = response.orderId
                                    st._tp_limit_price = limit_price
                                    dir_str = "LONG" if st.position == 1 else "SHORT"
                                    print(f"[{now_str}] [{sym} TP-LIMIT] {dir_str} x{real_size} "
                                          f"limit @ {limit_price:.2f}")
                                else:
                                    print(f"[{now_str}] [{sym} TP-LIMIT] Order failed: {response}")
                            else:
                                print(f"[{now_str}] [{sym} TP-LIMIT] No open position found")
                        except Exception as e:
                            print(f"[{now_str}] [{sym} TP-LIMIT] Error: {e}")
                    elif action[0] == "flatten":
                        reason = action[2] if len(action) > 2 else "signal"
                        await st._flatten(action[1], reason)

            now = time.time()
            tick_gap = now - self.last_real_tick_time

            if not price_changed and tick_gap > TICK_HEALTH_TIMEOUT:
                self._reconnect_count += 1
                if self._reconnect_count > self._max_reconnects:
                    print(f"[HEALTH] {self._reconnect_count} reconnect attempts failed — restarting")
                    self.save_all_state()
                    import sys
                    sys.exit(1)
                stale_prices = ", ".join(f"{s}={p:.2f}" for s, p in self._last_seen_price.items())
                print(f"[HEALTH] Price unchanged for {tick_gap:.0f}s — reconnecting "
                      f"(attempt {self._reconnect_count}/{self._max_reconnects})")
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
                except Exception as e:
                    print(f"[HEALTH] Reconnect failed: {e}")
                    await asyncio.sleep(10)
                continue

            if now - last_save > save_interval:
                self.save_all_state()
                last_save = now

            if now - last_status > status_interval:
                for sym, st in self.states.items():
                    pos_str = {0: "FLAT", 1: "LONG", -1: "SHORT"}[st.position]
                    trend = st.mms.trend_bias or "neutral"
                    struct = st.mms.structure_bias or "none"
                    print(f"  [{sym} @ {datetime.now(ET).strftime('%H:%M:%S')}]")
                    print(f"    Price: {st.last_price:.2f} | Trend: {trend} | Structure: {struct}")
                    print(f"    Position: {pos_str} x{st.contracts_held} | "
                          f"P&L: ${st.live_pnl:.2f} | Trades: {st._trade_count}")
                    print(f"    FVGs: {len(st.mms.active_fvgs)} | "
                          f"EQH: {len(st.mms.eqh_levels)} | EQL: {len(st.mms.eql_levels)}")
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
    parser = argparse.ArgumentParser(description="TopstepX MMS Bot")
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

    print(f"[BOT] MMS (Market Maker Model) Bot v2")
    print(f"[BOT] Strategy: Elliott 5-Wave Reversal + FVG tap at wave 2/4")
    print(f"[BOT] Timeframes: 15m trend / 5m structure+waves / 1m entry")
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

    signal_mod.signal(signal_mod.SIGINT, handle_signal)
    signal_mod.signal(signal_mod.SIGTERM, handle_signal)

    def thread_watchdog():
        while not stopped:
            time.sleep(30)
            if not current_bot:
                continue
            real_gap = time.time() - current_bot.last_real_tick_time if current_bot.last_real_tick_time > 0 else 0
            loop_gap = time.time() - current_bot.last_tick_time if current_bot.last_tick_time > 0 else 0
            if in_session() and not in_blackout():
                if real_gap > WATCHDOG_TIMEOUT:
                    print(f"[THREAD-WATCHDOG] No real ticks for {real_gap:.0f}s — force-killing")
                    current_bot.save_all_state()
                    os._exit(1)
            elif loop_gap > WATCHDOG_TIMEOUT + 120:
                print(f"[THREAD-WATCHDOG] Event loop appears dead — force-killing")
                current_bot.save_all_state()
                os._exit(1)

    wd = threading.Thread(target=thread_watchdog, daemon=True)
    wd.start()

    retry_delay = 30
    while not stopped:
        bot = MMSBot(
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
                          f"STATUS|MMS bot crashed, restarting in {retry_delay}s")
            retry_delay = min(retry_delay * 2, 300)
        finally:
            bot.save_all_state()
            loop.close()

        if not stopped:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
