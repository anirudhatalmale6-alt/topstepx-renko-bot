"""
TopstepX Renko AO+Stoch+RSI Strategy Bot (LIVE)
Multi-symbol support: runs multiple instruments on one connection.

Strategy: Renko + Awesome Oscillator + Stochastic + RSI (Buy&Sell Strategy)
- LONG: Stoch K < 20 AND RSI < 30 AND AO rising
- SHORT: Stoch K > 80 AND RSI > 70 AND AO falling
- EXIT: ATR-based TP/SL (ATR = brick_size)

Usage:
    python renko_bot.py --symbols "NQ:3:1:ntfy-topic" --tick-interval 1
"""

import asyncio
import argparse
import signal
import json
import os
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, time as dtime

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


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(18, 0, 0)
SESSION_END = dtime(16, 0)

TRADING_DAYS = [0, 1, 2, 3, 4, 6]

AO_FAST = 5
AO_SLOW = 34
STOCH_K = 14
STOCH_D = 3
STOCH_SMOOTH = 3
RSI_PERIOD = 10

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
        return t >= SESSION_START or t < SESSION_END
    return SESSION_START <= t < SESSION_END


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
        self.brick_highs = []
        self.brick_lows = []
        self.brick_hl2s = []

        self.ao = None
        self.prev_ao = None
        self.stoch_k = None
        self.rsi = None
        self.avg_gain = None
        self.avg_loss = None

        self.stop_loss = None
        self.take_profit = None

        self.last_price = 0.0

        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None

        self.live_pnl = 0.0

        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"trade_log_{symbol}.jsonl"
        )

        self.ctx = None
        self.last_new_bar_time = None

    def save_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "brick_closes": self.brick_closes[-100:],
            "brick_highs": self.brick_highs[-100:],
            "brick_lows": self.brick_lows[-100:],
            "brick_hl2s": self.brick_hl2s[-100:],
            "avg_gain": self.avg_gain,
            "avg_loss": self.avg_loss,
            "last_price": self.last_price,
            "renko_last_close": self.renko.last_close,
            "renko_direction": self.renko.direction,
            "renko_brick_count": self.renko.brick_count,
            "position": self.position,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "live_pnl": self.live_pnl,
            "saved_at": time.time(),
        }

    def restore_state(self, state: dict):
        if time.time() - state.get("saved_at", 0) > 600:
            return False
        self.brick_closes = state.get("brick_closes", [])
        self.brick_highs = state.get("brick_highs", [])
        self.brick_lows = state.get("brick_lows", [])
        self.brick_hl2s = state.get("brick_hl2s", [])
        self.avg_gain = state.get("avg_gain")
        self.avg_loss = state.get("avg_loss")
        self.last_price = state.get("last_price", 0.0)
        self.renko.last_close = state.get("renko_last_close")
        self.renko.direction = state.get("renko_direction", 0)
        self.renko.brick_count = state.get("renko_brick_count", 0)
        self.position = state.get("position", 0)
        self.entry_price = state.get("entry_price", 0.0)
        self.stop_loss = state.get("stop_loss")
        self.take_profit = state.get("take_profit")
        self.live_pnl = state.get("live_pnl", 0.0)
        return True

    def _add_brick_data(self, brick_open, brick_close):
        h = max(brick_open, brick_close)
        l = min(brick_open, brick_close)
        self.brick_closes.append(brick_close)
        self.brick_highs.append(h)
        self.brick_lows.append(l)
        self.brick_hl2s.append((h + l) / 2.0)

    def _calc_indicators(self):
        n = len(self.brick_closes)
        if n != len(self.brick_highs):
            return
        self.prev_ao = self.ao
        if n >= AO_SLOW:
            fast_sma = sum(self.brick_hl2s[-AO_FAST:]) / AO_FAST
            slow_sma = sum(self.brick_hl2s[-AO_SLOW:]) / AO_SLOW
            self.ao = (fast_sma - slow_sma) * 1000
        if n >= STOCH_K and len(self.brick_highs) >= STOCH_K:
            raw_stochs = []
            need = STOCH_K + STOCH_D - 1
            hn = len(self.brick_highs)
            start = max(0, hn - need)
            for i in range(start, hn):
                lo = min(self.brick_lows[max(0, i - STOCH_K + 1):i + 1])
                hi = max(self.brick_highs[max(0, i - STOCH_K + 1):i + 1])
                if hi == lo:
                    raw_stochs.append(50.0)
                else:
                    raw_stochs.append((self.brick_closes[i] - lo) / (hi - lo) * 100.0)
            if len(raw_stochs) >= STOCH_D:
                k_values = []
                for i in range(STOCH_D - 1, len(raw_stochs)):
                    k_values.append(sum(raw_stochs[i - STOCH_D + 1:i + 1]) / STOCH_D)
                self.stoch_k = k_values[-1] if k_values else None
        if n >= RSI_PERIOD + 1:
            if self.avg_gain is None:
                gains = []
                losses = []
                for i in range(1, RSI_PERIOD + 1):
                    change = self.brick_closes[i] - self.brick_closes[i - 1]
                    gains.append(max(change, 0))
                    losses.append(max(-change, 0))
                self.avg_gain = sum(gains) / RSI_PERIOD
                self.avg_loss = sum(losses) / RSI_PERIOD
                for i in range(RSI_PERIOD + 1, n):
                    change = self.brick_closes[i] - self.brick_closes[i - 1]
                    self.avg_gain = (self.avg_gain * (RSI_PERIOD - 1) + max(change, 0)) / RSI_PERIOD
                    self.avg_loss = (self.avg_loss * (RSI_PERIOD - 1) + max(-change, 0)) / RSI_PERIOD
            else:
                change = self.brick_closes[-1] - self.brick_closes[-2]
                self.avg_gain = (self.avg_gain * (RSI_PERIOD - 1) + max(change, 0)) / RSI_PERIOD
                self.avg_loss = (self.avg_loss * (RSI_PERIOD - 1) + max(-change, 0)) / RSI_PERIOD
            if self.avg_loss == 0:
                self.rsi = 100.0
            else:
                rs = self.avg_gain / self.avg_loss
                self.rsi = 100.0 - (100.0 / (1.0 + rs))

    async def seed_history(self):
        data = await self.ctx.data.get_data("1sec", bars=800)
        if data is None or len(data) == 0:
            print(f"[{self.symbol}] No historical 1sec data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[{self.symbol}] Seeding from {len(rows)} historical 1sec bars...")

        self.brick_closes = []
        self.brick_highs = []
        self.brick_lows = []
        self.brick_hl2s = []
        self.avg_gain = None
        self.avg_loss = None
        self.ao = None
        self.prev_ao = None
        self.stoch_k = None
        self.rsi = None

        for row in rows:
            close = float(row["close"])
            bricks = self.renko.feed_close(close)
            for brick in bricks:
                self._add_brick_data(brick[0], brick[1])

        self._calc_indicators()

        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"
        print(f"  [{self.symbol}] Renko: {self.renko.brick_count} bricks, {dir_str}, ref={self.renko.last_close:.2f}")
        print(f"  [{self.symbol}] Data: {len(self.brick_closes)} brick closes")
        ao_str = f"{self.ao:.2f}" if self.ao is not None else "N/A"
        k_str = f"{self.stoch_k:.2f}" if self.stoch_k is not None else "N/A"
        rsi_str = f"{self.rsi:.2f}" if self.rsi is not None else "N/A"
        print(f"  [{self.symbol}] AO: {ao_str} | Stoch K: {k_str} | RSI: {rsi_str}")

    def print_status(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"
        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"

        print(f"  [{self.symbol} @ {now}]")
        print(f"    Renko: {dir_str} | last_close={self.renko.last_close:.2f} | bricks={self.renko.brick_count}")
        ao_str = f"{self.ao:.2f}" if self.ao is not None else "N/A"
        k_str = f"{self.stoch_k:.2f}" if self.stoch_k is not None else "N/A"
        rsi_str = f"{self.rsi:.2f}" if self.rsi is not None else "N/A"
        print(f"    AO: {ao_str} | Stoch K: {k_str} | RSI: {rsi_str}")
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
        now = datetime.now(ET).strftime("%H:%M:%S")

        # Check TP/SL every tick (0.5s)
        if self.position != 0 and self.stop_loss is not None and self.take_profit is not None:
            if self.position == 1:
                if price <= self.stop_loss:
                    pnl = (price - self.entry_price) * self.point_value * self.qty
                    print(f"[{now}] [{self.symbol} STOP LOSS] Price {price:.2f} hit SL {self.stop_loss:.2f} | P&L: ${pnl:+.2f}")
                    await self._flatten(price, reason="STOP_LOSS")
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "FLAT", self.symbol, price, 0), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
                    return True
                elif price >= self.take_profit:
                    pnl = (price - self.entry_price) * self.point_value * self.qty
                    print(f"[{now}] [{self.symbol} TAKE PROFIT] Price {price:.2f} hit TP {self.take_profit:.2f} | P&L: ${pnl:+.2f}")
                    await self._flatten(price, reason="TAKE_PROFIT")
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "FLAT", self.symbol, price, 0), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
                    return True
            elif self.position == -1:
                if price >= self.stop_loss:
                    pnl = (self.entry_price - price) * self.point_value * self.qty
                    print(f"[{now}] [{self.symbol} STOP LOSS] Price {price:.2f} hit SL {self.stop_loss:.2f} | P&L: ${pnl:+.2f}")
                    await self._flatten(price, reason="STOP_LOSS")
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "FLAT", self.symbol, price, 0), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
                    return True
                elif price <= self.take_profit:
                    pnl = (self.entry_price - price) * self.point_value * self.qty
                    print(f"[{now}] [{self.symbol} TAKE PROFIT] Price {price:.2f} hit TP {self.take_profit:.2f} | P&L: ${pnl:+.2f}")
                    await self._flatten(price, reason="TAKE_PROFIT")
                    threading.Thread(target=send_signals, args=(
                        self.tg_token, self.tg_chat, self.tg_keys,
                        "FLAT", self.symbol, price, 0), kwargs={"ntfy_topic": self.ntfy_topic}, daemon=True).start()
                    return True

        # Feed Renko bricks every tick_interval
        now_ts = time.time()
        if now_ts - self.last_tick_time < self.tick_interval:
            return True
        self.last_tick_time = now_ts
        self.last_new_bar_time = now_ts

        bricks = self.renko.feed_close(price)
        if not bricks:
            return True

        for b in bricks:
            brick_dir = b[2]
            color = "BULLISH" if brick_dir == 1 else "BEARISH"
            self._add_brick_data(b[0], b[1])
            self._calc_indicators()

            ind_str = ""
            if self.ao is not None:
                ind_str += f" | AO: {self.ao:.1f}"
            if self.stoch_k is not None:
                ind_str += f" K: {self.stoch_k:.1f}"
            if self.rsi is not None:
                ind_str += f" RSI: {self.rsi:.1f}"
            print(f"[{now}] [{self.symbol} RENKO] {color} brick #{self.renko.brick_count}: {b[0]:.2f} -> {b[1]:.2f}{ind_str}")

            if self.position != 0:
                continue

            if self.ao is None or self.prev_ao is None or self.stoch_k is None or self.rsi is None:
                continue

            # LONG: Stoch K < 20 AND RSI < 30 AND AO rising
            if self.stoch_k < 20 and self.rsi < 30 and self.ao > self.prev_ao:
                atr = self.brick_size
                sl = price - atr
                tp = price + atr
                print(f"[{now}] [{self.symbol} SIGNAL] LONG | K={self.stoch_k:.1f}<20 RSI={self.rsi:.1f}<30 AO rising | SL={sl:.2f} TP={tp:.2f}")
                self.stop_loss = sl
                self.take_profit = tp
                await self._enter_long(price)

            # SHORT: Stoch K > 80 AND RSI > 70 AND AO falling
            elif self.stoch_k > 80 and self.rsi > 70 and self.ao < self.prev_ao:
                atr = self.brick_size
                sl = price + atr
                tp = price - atr
                print(f"[{now}] [{self.symbol} SIGNAL] SHORT | K={self.stoch_k:.1f}>80 RSI={self.rsi:.1f}>70 AO falling | SL={sl:.2f} TP={tp:.2f}")
                self.stop_loss = sl
                self.take_profit = tp
                await self._enter_short(price)

        return True

    async def _enter_long(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING LONG @ {price:.2f} | SL={self.stop_loss:.2f} TP={self.take_profit:.2f} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=0,
                size=self.qty,
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
                return False
        except Exception as e:
            print(f"[{self.symbol}] Order ERROR: {e}")
            return False
        return True

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [{self.symbol}] >>> ENTERING SHORT @ {price:.2f} | SL={self.stop_loss:.2f} TP={self.take_profit:.2f} | P&L: ${self.live_pnl:.2f}")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=1,
                size=self.qty,
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
                return False
        except Exception as e:
            print(f"[{self.symbol}] Order ERROR: {e}")
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
        self.stop_loss = None
        self.take_profit = None

        try:
            result = await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id,
                ),
                timeout=5.0,
            )
            print(f"[{self.symbol}] Position closed via close_position_direct")
        except Exception as e:
            print(f"[{self.symbol}] close_position_direct failed ({e}), using market order fallback")
            try:
                close_side = 1 if direction == "LONG" else 0
                response = await self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=close_side,
                    size=self.qty,
                )
                if response.success:
                    print(f"[{self.symbol}] Position closed (fallback). ID: {response.orderId}")
                else:
                    print(f"[{self.symbol}] CLOSE FAILED: {response.errorMessage}")
                    return False
            except Exception as e2:
                print(f"[{self.symbol}] Close ERROR: {e2}")
                return False

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
            "ao": self.ao,
            "stoch_k": self.stoch_k,
            "rsi": self.rsi,
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
                        ao_s = f"{st.ao:.2f}" if st.ao is not None else "N/A"
                        print(f"  [{sym}] Restored: AO={ao_s}, Bricks={st.renko.brick_count}")
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
        print(f"[BOT] Renko AO+Stoch+RSI Strategy - LIVE MODE")
        print(f"[BOT] Tick interval: {self.tick_interval}s (samples price every {self.tick_interval} seconds)")
        print(f"[BOT] AO({AO_FAST},{AO_SLOW}) | Stoch({STOCH_K},{STOCH_D},{STOCH_SMOOTH}) | RSI({RSI_PERIOD})")
        print(f"[BOT] Symbols: {', '.join(symbols)}")
        for sym, st in self.states.items():
            print(f"[BOT]   {sym}: brick={st.brick_size}, qty={st.qty}, pv=${st.point_value}/pt" +
                  (f", ntfy={st.ntfy_topic}" if st.ntfy_topic else ""))
        print(f"[BOT] LONG: K<20 + RSI<30 + AO rising | SHORT: K>80 + RSI>70 + AO falling")
        print(f"[BOT] EXIT: ATR-based TP/SL (ATR = brick_size)")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - {SESSION_END.strftime('%H:%M')} ET ({trading_day_str})")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        print()

        self.suite = await TradingSuite.create(
            instruments=symbols,
            timeframes=["1sec", "15min"],
            initial_days=1,
        )

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

            if not restored or st.ao is None:
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
        print(f"[BOT] Trading LIVE - AO+Stoch+RSI ({', '.join(symbols)})")
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
                if st.is_data_stale(120):
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [STALE] {sym} no new data for 2+ min - reconnecting")
                    self._notify_status(f"STATUS|{sym} data stale, reconnecting ({now} ET)")
                    if time.time() - self.last_reconnect_time > 120:
                        await self._auto_reconnect()
                    break

        if time.time() - self.last_state_save > 30:
            self.save_all_state()
            self.last_state_save = time.time()

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
    parser = argparse.ArgumentParser(description="TopstepX Renko AO+Stoch+RSI Bot (Multi-Symbol)")
    parser.add_argument("--symbol", default="", help="Single symbol (backward compat)")
    parser.add_argument("--symbols", default="", help="Multi-symbol config: 'NQ:3.0:1:ntfy,ES:2.0:1'")
    parser.add_argument("--qty", type=int, default=1, help="Qty for single --symbol mode")
    parser.add_argument("--brick-size", type=float, default=3.0, help="Brick size for single --symbol mode")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--ntfy-topic", default="", help="ntfy.sh topic (single --symbol mode)")
    parser.add_argument("--tick-interval", type=int, default=10, help="Seconds between price samples (default: 10)")
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
        symbol_configs = [{"symbol": "NQ", "brick_size": 3.0, "qty": 1, "ntfy_topic": ""}]

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

    while not stopped:
        bot = RenkoBot(
            symbol_configs=symbol_configs,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            tg_keys=keys,
            tick_interval=args.tick_interval,
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
