"""
TopstepX Renko 20 SMA + Ichimoku Cloud Strategy Bot (LIVE)

Strategy: 1sec Renko (bricks from candle CLOSE) + 20 SMA + Ichimoku Cloud
- TRIGGER: Ghost candle (real-time price) crosses 20 SMA on 1sec Renko
- DIRECTION: Determined by Ichimoku cloud (above cloud = LONG, below cloud = SHORT)
- EXIT: Ghost candle crosses 20 SMA again OR trailing profit
- TRAILING: Stepped locks ($70→$50, $100→$80, $130→$110)
- No stop loss

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python renko_bot.py --symbol NQ --qty 1 --brick-size 0.25
"""

import asyncio
import argparse
import signal
import json
import os
import time
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
    """Traditional Renko engine matching TradingView's calculation.
    Builds bricks from CLOSE prices only.
    """

    def __init__(self, brick_size: float, label: str = ""):
        self.brick_size = brick_size
        self.label = label
        self.last_close = None
        self.direction = 0      # 1=BULLISH, -1=BEARISH, 0=not started
        self.brick_count = 0

    def initialize(self, price: float):
        self.last_close = round(price / self.brick_size) * self.brick_size

    def feed_close(self, close_price: float) -> list:
        """Feed a candle close price. Returns list of new bricks: [(open, close, direction), ...]"""
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

SESSION_START = dtime(18, 0, 0)    # 6:00 PM ET
SESSION_END = dtime(16, 0)         # 4:00 PM ET (next day - wraps midnight)

POINT_VALUE = 20.0  # NQ: $20 per point per contract

TRADING_DAYS = [0, 1, 2, 3, 4, 6]  # Sun-Fri (Sun 6PM start, Fri 4PM end)

SMA_PERIOD = 20  # BB middle line = 20 SMA
EMA_BUFFER = 3.0  # (legacy name) buffer zone uses brick_size dynamically

# Ichimoku Cloud parameters (on Renko bricks)
TENKAN_PERIOD = 9
KIJUN_PERIOD = 26
SENKOU_B_PERIOD = 52
CLOUD_DISPLACEMENT = 26

# Stepped trailing profit: (trigger_level, lock_floor)
# When profit reaches trigger_level, lock_floor becomes the exit floor
TRAIL_STEPS = [
    (70.0, 50.0),    # Hit $70 → lock $50
    (100.0, 80.0),   # Hit $100 → lock $80
    (130.0, 110.0),  # Hit $130 → lock $110
]


def in_session() -> bool:
    now = datetime.now(ET)
    if now.weekday() not in TRADING_DAYS:
        return False
    t = now.time()
    if SESSION_START > SESSION_END:
        return t >= SESSION_START or t < SESSION_END
    return SESSION_START <= t < SESSION_END


# ============================================================
# Main Bot
# ============================================================

class RenkoBot:
    def __init__(self, symbol: str, qty: int = 1,
                 brick_size: float = 0.25,
                 tg_token: str = "", tg_chat: str = "", tg_keys: list = None,
                 ntfy_topic: str = ""):
        self.symbol = symbol
        self.qty = qty
        self.brick_size = brick_size
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.ntfy_topic = ntfy_topic

        # 1sec Renko engine (main strategy)
        self.renko = RenkoEngine(brick_size, "1sec")

        # Bar tracking
        self.last_bar_time = None

        # 20 SMA on 1sec Renko brick closes
        self.ema_closes = []     # list of 1sec Renko brick close prices
        self.ema_9 = None        # current 20 SMA value

        # Ghost candle state
        self.ghost_above_ema = None  # True/False/None
        self.last_price = 0.0

        # Ichimoku cloud state
        self.senkou_a = None  # Senkou Span A (current cloud top/bottom)
        self.senkou_b = None  # Senkou Span B (current cloud top/bottom)
        self.cloud_position = None  # "ABOVE", "BELOW", "IN_CLOUD", None

        # Trailing profit (stepped)
        self.max_profit = 0.0
        self.trailing_active = False
        self.trail_lock_floor = 0.0   # current lock floor (moves up with steps)
        self.trail_step_idx = -1      # index of highest reached step in TRAIL_STEPS

        # Position
        self.position = 0       # 1=long, -1=short, 0=flat
        self.entry_price = 0.0
        self.entry_time = None

        # Trade log
        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "trade_log.jsonl"
        )

        # P&L
        self.live_pnl = 0.0

        # Session
        self.was_in_session = False

        # Connection health
        self.last_price_time = None
        self.connection_alive = True
        self.disconnect_alert_sent = False
        self.STALE_THRESHOLD = 60
        self.RECONNECT_THRESHOLD = 90
        self.reconnecting = False
        self.last_reconnect_time = 0

        # SDK
        self.suite = None
        self.ctx = None
        self.running = False

    def _calc_ema(self):
        """Calculate 20 SMA (BB middle line) from brick closes."""
        if not self.ema_closes:
            return
        # Use last SMA_PERIOD closes for the average
        window = self.ema_closes[-SMA_PERIOD:]
        self.ema_9 = sum(window) / len(window)

    def _calc_ichimoku(self):
        """Calculate Ichimoku cloud (Senkou Span A & B) from Renko brick closes.

        Uses displaced values: the cloud plotted 26 periods ahead on a chart
        means we look at values calculated 26 bricks AGO for the current cloud.
        """
        closes = self.ema_closes
        n = len(closes)

        # Need enough data: SENKOU_B_PERIOD + CLOUD_DISPLACEMENT bricks minimum
        min_needed = SENKOU_B_PERIOD + CLOUD_DISPLACEMENT
        if n < min_needed:
            return

        # For the CURRENT cloud position, we need the Senkou values that were
        # calculated CLOUD_DISPLACEMENT periods ago (they were projected forward)
        # So we look at closes up to index [n - CLOUD_DISPLACEMENT]
        offset = n - CLOUD_DISPLACEMENT

        # Tenkan-sen (conversion line) = (highest high + lowest low) / 2 over TENKAN_PERIOD
        tenkan_window = closes[offset - TENKAN_PERIOD:offset]
        tenkan = (max(tenkan_window) + min(tenkan_window)) / 2.0

        # Kijun-sen (base line) = (highest high + lowest low) / 2 over KIJUN_PERIOD
        kijun_window = closes[offset - KIJUN_PERIOD:offset]
        kijun = (max(kijun_window) + min(kijun_window)) / 2.0

        # Senkou Span A = (Tenkan + Kijun) / 2, displaced forward by CLOUD_DISPLACEMENT
        self.senkou_a = (tenkan + kijun) / 2.0

        # Senkou Span B = (highest high + lowest low) / 2 over SENKOU_B_PERIOD, displaced forward
        senkou_b_window = closes[offset - SENKOU_B_PERIOD:offset]
        self.senkou_b = (max(senkou_b_window) + min(senkou_b_window)) / 2.0

    def _get_cloud_position(self, price: float) -> str:
        """Determine price position relative to Ichimoku cloud."""
        if self.senkou_a is None or self.senkou_b is None:
            return None

        cloud_top = max(self.senkou_a, self.senkou_b)
        cloud_bottom = min(self.senkou_a, self.senkou_b)

        if price > cloud_top:
            return "ABOVE"
        elif price < cloud_bottom:
            return "BELOW"
        else:
            return "IN_CLOUD"

    async def run(self):
        from project_x_py import TradingSuite

        print(f"[BOT] Renko 20 SMA + Ichimoku Cloud Strategy - LIVE MODE")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Brick size: {self.brick_size} (Traditional)")
        print(f"[BOT] Strategy: 1sec Renko + 20 SMA trigger + Ichimoku Cloud direction")
        print(f"[BOT] TRIGGER: Ghost candle crosses 20 SMA")
        print(f"[BOT] DIRECTION: Ichimoku cloud (above=LONG, below=SHORT, in cloud=SKIP)")
        print(f"[BOT] Ichimoku: Tenkan={TENKAN_PERIOD}, Kijun={KIJUN_PERIOD}, SenkouB={SENKOU_B_PERIOD}, Disp={CLOUD_DISPLACEMENT}")
        print(f"[BOT] EXIT: Ghost candle crosses SMA again OR trailing profit")
        steps_str = " → ".join(f"${t}→lock${l}" for t, l in TRAIL_STEPS)
        print(f"[BOT] Trail steps: {steps_str}")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - {SESSION_END.strftime('%H:%M')} ET ({trading_day_str})")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        print()

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["1sec", "15min"],
            initial_days=1,
        )
        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        price = await self.ctx.data.get_current_price()
        if price:
            self.renko.initialize(price)
            self.last_price = price
            print(f"[BOT] 1sec Renko initialized at {price:.2f}")

        await self._seed_history()

        print()
        self.running = True
        self.was_in_session = in_session()

        self._print_status()

        print(f"\n[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Trading LIVE - 20 SMA + Ichimoku Cloud")
        print(f"[BOT] Press Ctrl+C to stop\n")

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _seed_history(self):
        """Feed historical 10sec bars to warm up Renko + calculate initial 20 SMA."""
        data = await self.ctx.data.get_data("1sec", bars=800)
        if data is None or len(data) == 0:
            print("[BOT] No historical 1sec data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[BOT] Seeding 1sec Renko from {len(rows)} historical bars...")

        for row in rows:
            close = float(row["close"])
            bricks = self.renko.feed_close(close)
            for brick in bricks:
                self.ema_closes.append(brick[1])

        # Calculate initial SMA and Ichimoku from all historical brick closes
        if self.ema_closes:
            self._calc_ema()
            self._calc_ichimoku()

        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"
        print(f"  1sec Renko: {self.renko.brick_count} bricks, {dir_str}, ref={self.renko.last_close:.2f}")
        print(f"  SMA-20 data points: {len(self.ema_closes)} brick closes")
        if self.ema_9:
            print(f"  SMA-20 value: {self.ema_9:.2f}")
        else:
            print(f"  SMA-20: not enough data yet (need {SMA_PERIOD} bricks)")
        if self.senkou_a is not None:
            cloud_top = max(self.senkou_a, self.senkou_b)
            cloud_bottom = min(self.senkou_a, self.senkou_b)
            print(f"  Ichimoku Cloud: top={cloud_top:.2f}, bottom={cloud_bottom:.2f}")
        else:
            min_needed = SENKOU_B_PERIOD + CLOUD_DISPLACEMENT
            print(f"  Ichimoku Cloud: not enough data yet (need {min_needed} bricks, have {len(self.ema_closes)})")

    def _print_status(self):
        """Print current strategy status."""
        now = datetime.now(ET).strftime("%H:%M:%S")
        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"
        dir_str = "BULLISH" if self.renko.direction == 1 else "BEARISH" if self.renko.direction == -1 else "NONE"

        print(f"\n  [STATUS @ {now}]")
        print(f"  1sec Renko: {dir_str} | last_close={self.renko.last_close:.2f} | bricks={self.renko.brick_count}")
        if self.ema_9:
            print(f"  20 SMA: {self.ema_9:.2f}")
            ghost_str = "ABOVE" if self.ghost_above_ema else "BELOW" if self.ghost_above_ema is False else "UNKNOWN"
            print(f"  Ghost vs SMA: {ghost_str}")
        else:
            print(f"  20 SMA: waiting for data ({len(self.ema_closes)}/{SMA_PERIOD} bricks)")
        if self.senkou_a is not None:
            cloud_top = max(self.senkou_a, self.senkou_b)
            cloud_bottom = min(self.senkou_a, self.senkou_b)
            cloud_pos = self._get_cloud_position(self.last_price) or "UNKNOWN"
            print(f"  Ichimoku Cloud: top={cloud_top:.2f}, bottom={cloud_bottom:.2f} | Price: {cloud_pos}")
        else:
            print(f"  Ichimoku Cloud: waiting for data")
        print(f"  Position: {pos_str} | P&L: ${self.live_pnl:.2f}")

    async def _auto_reconnect(self):
        """Reconnect WebSocket without restarting Renko engine (preserves brick state)."""
        from project_x_py import TradingSuite
        self.reconnecting = True
        self.last_reconnect_time = time.time()
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [RECONNECT] Auto-reconnecting (Renko + EMA preserved)...")
        send_telegram(self.tg_token, self.tg_chat, f"STATUS|Auto-reconnecting ({now} ET)")

        if self.suite:
            try:
                await self.suite.disconnect()
            except Exception:
                pass

        try:
            self.suite = await TradingSuite.create(
                instruments=self.symbol,
                timeframes=["1sec", "15min"],
                initial_days=1,
            )
            self.ctx = self.suite[self.symbol]
            self.last_1min_time = None
            self.last_price_time = time.time()
            self.connection_alive = True
            self.disconnect_alert_sent = False
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] WebSocket restored, Renko+EMA intact")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|RECONNECTED ({now} ET)")

            # SAFETY: Flatten if holding position during disconnect
            if self.position != 0:
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"[{now}] [SAFETY] Was {direction} during disconnect - FLATTENING")
                send_telegram(self.tg_token, self.tg_chat,
                             f"STATUS|SAFETY FLATTEN - was {direction} ({now} ET)")
                try:
                    price = await self.ctx.data.get_current_price()
                    if price:
                        await self._flatten(price, reason="SAFETY_RECONNECT")
                        send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                     "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)
                except Exception as e:
                    print(f"[{now}] [SAFETY] Flatten failed: {e} - will retry next tick")

        except Exception as e:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [RECONNECT] Failed: {e} - will retry in 2 min")
            self.suite = None
            self.ctx = None
        finally:
            self.reconnecting = False

    async def _tick(self):
        # If disconnected, try reconnect
        if self.ctx is None:
            if in_session() and not self.reconnecting:
                if time.time() - self.last_reconnect_time > 120:
                    await self._auto_reconnect()
            return

        price = await self.ctx.data.get_current_price()
        now_ts = time.time()

        if price is None:
            if self.last_price_time and in_session():
                elapsed = now_ts - self.last_price_time
                if elapsed > self.STALE_THRESHOLD and not self.disconnect_alert_sent:
                    self.connection_alive = False
                    self.disconnect_alert_sent = True
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [ALERT] No price data for {int(elapsed)}s")
                    send_telegram(self.tg_token, self.tg_chat, f"STATUS|DISCONNECTED ({now} ET)")
                if elapsed > self.RECONNECT_THRESHOLD and not self.reconnecting:
                    if now_ts - self.last_reconnect_time > 120:
                        await self._auto_reconnect()
            return

        self.last_price_time = now_ts
        self.last_price = price
        if not self.connection_alive:
            self.connection_alive = True
            self.disconnect_alert_sent = False
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [ALERT] Price data restored")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|RECONNECTED ({now} ET)")

        # Session boundaries
        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        if sess_ended:
            if self.position != 0:
                print(f"[SESSION] Session ended - flattening")
                await self._flatten(price, reason="SESSION_END")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] Disconnecting until next session...")
            if self.suite:
                try:
                    await self.suite.disconnect()
                except Exception:
                    pass
                self.suite = None
                self.ctx = None
            self.was_in_session = currently_in_session
            return

        sess_started = not self.was_in_session and currently_in_session
        if sess_started:
            self.live_pnl = 0.0
            self.bar_count = 0
            if self.suite is None:
                from project_x_py import TradingSuite
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [SESSION] Reconnecting...")
                self.suite = await TradingSuite.create(
                    instruments=self.symbol,
                    timeframes=["1sec", "15min"],
                    initial_days=1,
                )
                self.ctx = self.suite[self.symbol]
                await self._seed_history()
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] New session started - LIVE")
            self._print_status()

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # ---- Check for new 1min bar → feed 1sec Renko ----
        now = datetime.now(ET).strftime("%H:%M:%S")
        new_brick = False

        data_10s = await self.ctx.data.get_data("1sec", bars=1)
        if data_10s is not None and len(data_10s) > 0:
            rows = list(data_10s.iter_rows(named=True))
            last_row = rows[-1]
            bar_time = last_row.get("timestamp") or last_row.get("time")

            if bar_time != self.last_bar_time:
                self.last_bar_time = bar_time
                close_10s = float(last_row["close"])

                bricks = self.renko.feed_close(close_10s)
                if bricks:
                    new_brick = True
                    for b in bricks:
                        color = "BULLISH" if b[2] == 1 else "BEARISH"
                        self.ema_closes.append(b[1])
                        self._calc_ema()
                        self._calc_ichimoku()
                        cloud_str = ""
                        if self.senkou_a is not None:
                            cloud_top = max(self.senkou_a, self.senkou_b)
                            cloud_bottom = min(self.senkou_a, self.senkou_b)
                            cloud_str = f" | Cloud: {cloud_bottom:.2f}-{cloud_top:.2f}"
                        print(f"[{now}] [RENKO 1s] {color} brick #{self.renko.brick_count}: {b[0]:.2f} -> {b[1]:.2f} | SMA-20: {self.ema_9:.2f}{cloud_str}" if self.ema_9 else f"[{now}] [RENKO 1s] {color} brick #{self.renko.brick_count}: {b[0]:.2f} -> {b[1]:.2f}")

        # ---- Ghost candle vs 20 SMA check (every tick) ----
        if self.ema_9 is None:
            return

        prev_ghost = self.ghost_above_ema
        # Use buffer zone: price must be > EMA + buffer to be "above", < EMA - buffer to be "below"
        # When in the buffer zone, keep previous state (no flip)
        if price > self.ema_9 + self.brick_size:
            self.ghost_above_ema = True
        elif price < self.ema_9 - self.brick_size:
            self.ghost_above_ema = False
        # else: in buffer zone, keep prev_ghost state (no change)

        # Detect crossover
        crossed = False
        cross_direction = 0
        if prev_ghost is not None and self.ghost_above_ema is not None and prev_ghost != self.ghost_above_ema:
            crossed = True
            cross_direction = 1 if self.ghost_above_ema else -1
            cross_str = "ABOVE (BULLISH)" if cross_direction == 1 else "BELOW (BEARISH)"
            diff = abs(price - self.ema_9)
            print(f"[{now}] [GHOST CROSS] Price {price:.2f} crossed SMA {self.ema_9:.2f} (diff {diff:.2f}) -> {cross_str}")

        # ---- Stepped trailing profit check ----
        if self.position != 0:
            unrealized = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
            if unrealized > self.max_profit:
                self.max_profit = unrealized

            # Check if we've reached a new trail step
            for i, (trigger, lock) in enumerate(TRAIL_STEPS):
                if unrealized >= trigger and i > self.trail_step_idx:
                    self.trail_step_idx = i
                    self.trail_lock_floor = lock
                    self.trailing_active = True
                    print(f"[{now}] [TRAIL] Step {i+1}: profit ${unrealized:.2f} >= ${trigger} → lock floor ${lock}")

            # Exit if profit drops below current lock floor
            if self.trailing_active and unrealized <= self.trail_lock_floor:
                print(f"[{now}] [TRAIL] Locking profit! ${unrealized:.2f} <= floor ${self.trail_lock_floor}")
                await self._flatten(price, reason=f"TRAIL_LOCK_${self.trail_lock_floor:.0f}")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)
                return

        # ---- Entry/Exit logic on ghost candle EMA cross ----
        if crossed:
            await self._live_logic(price, cross_direction)

    async def _live_logic(self, price: float, cross_direction: int):
        """Handle entries and exits on ghost candle SMA cross + Ichimoku cloud direction.

        TRIGGER: Any SMA cross (above or below)
        DIRECTION: Ichimoku cloud determines entry direction
          - Price ABOVE cloud → LONG
          - Price BELOW cloud → SHORT
          - Price IN cloud → SKIP (no entry)
        EXIT: Next SMA cross closes the current position
        """
        now = datetime.now(ET).strftime("%H:%M:%S")

        # Get cloud position
        cloud_pos = self._get_cloud_position(price)

        # EXIT: Any SMA cross while in a position → close it
        if self.position != 0:
            direction = "LONG" if self.position == 1 else "SHORT"
            await self._flatten(price, reason="SMA_CROSS_EXIT")
            send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                         "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)

        # ENTRY: Ichimoku cloud determines direction
        if cloud_pos is None:
            print(f"[{now}] [ICHIMOKU] Cloud not ready yet - skipping entry")
            return

        if cloud_pos == "IN_CLOUD":
            cloud_top = max(self.senkou_a, self.senkou_b)
            cloud_bottom = min(self.senkou_a, self.senkou_b)
            print(f"[{now}] [ICHIMOKU] Price {price:.2f} IN CLOUD ({cloud_bottom:.2f}-{cloud_top:.2f}) - skipping entry")
            return

        if cloud_pos == "ABOVE" and self.position <= 0:
            if self.position == -1:
                await self._flatten(price, reason="CLOUD_FLIP_LONG")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)
            cloud_top = max(self.senkou_a, self.senkou_b)
            print(f"[{now}] [ICHIMOKU] Price {price:.2f} ABOVE cloud (top={cloud_top:.2f}) → LONG")
            await self._enter_long(price)

        elif cloud_pos == "BELOW" and self.position >= 0:
            if self.position == 1:
                await self._flatten(price, reason="CLOUD_FLIP_SHORT")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)
            cloud_bottom = min(self.senkou_a, self.senkou_b)
            print(f"[{now}] [ICHIMOKU] Price {price:.2f} BELOW cloud (bottom={cloud_bottom:.2f}) → SHORT")
            await self._enter_short(price)

    async def _enter_long(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        cloud_str = ""
        if self.senkou_a is not None:
            cloud_top = max(self.senkou_a, self.senkou_b)
            cloud_str = f" | Cloud top: {cloud_top:.2f}"
        print(f"\n[{now}] [LIVE] >>> ENTERING LONG @ {price:.2f} | SMA: {self.ema_9:.2f}{cloud_str} | P&L: ${self.live_pnl:.2f}")
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
                self.max_profit = 0.0
                self.trailing_active = False
                self.trail_lock_floor = 0.0
                self.trail_step_idx = -1
                print(f"[LIVE] Order filled. ID: {response.orderId}")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "LONG", self.symbol, price, self.qty, ntfy_topic=self.ntfy_topic)
            else:
                print(f"[LIVE] Order FAILED: {response.errorMessage}")
                print(f"[LIVE] Triggering reconnect due to order failure...")
                await self._auto_reconnect()
        except Exception as e:
            print(f"[LIVE] Order ERROR: {e}")
            print(f"[LIVE] Triggering reconnect due to order exception...")
            await self._auto_reconnect()

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        cloud_str = ""
        if self.senkou_a is not None:
            cloud_bottom = min(self.senkou_a, self.senkou_b)
            cloud_str = f" | Cloud bottom: {cloud_bottom:.2f}"
        print(f"\n[{now}] [LIVE] >>> ENTERING SHORT @ {price:.2f} | SMA: {self.ema_9:.2f}{cloud_str} | P&L: ${self.live_pnl:.2f}")
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
                self.max_profit = 0.0
                self.trailing_active = False
                self.trail_lock_floor = 0.0
                self.trail_step_idx = -1
                print(f"[LIVE] Order filled. ID: {response.orderId}")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "SHORT", self.symbol, price, self.qty, ntfy_topic=self.ntfy_topic)
            else:
                print(f"[LIVE] Order FAILED: {response.errorMessage}")
                print(f"[LIVE] Triggering reconnect due to order failure...")
                await self._auto_reconnect()
        except Exception as e:
            print(f"[LIVE] Order ERROR: {e}")
            print(f"[LIVE] Triggering reconnect due to order exception...")
            await self._auto_reconnect()

    async def _flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        trade_pnl = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
        self.live_pnl += trade_pnl

        now = datetime.now(ET).strftime("%H:%M:%S")
        trail_str = f" | Trail: {'ACTIVE' if self.trailing_active else 'off'}" if self.trailing_active else ""
        print(f"\n[{now}] [LIVE] <<< EXITING {direction} @ {price:.2f} | Trade: ${trade_pnl:+.2f} | P&L: ${self.live_pnl:.2f} | {reason}{trail_str}")

        # Save entry price for logging before resetting
        saved_entry_price = self.entry_price

        # Reset position state BEFORE placing close order to prevent double-close on reconnect
        old_position = self.position
        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.max_profit = 0.0
        self.trailing_active = False
        self.trail_lock_floor = 0.0
        self.trail_step_idx = -1

        try:
            close_side = 1 if old_position == 1 else 0
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=close_side,
                size=self.qty,
            )
            if response.success:
                print(f"[LIVE] Position closed. ID: {response.orderId}")
            else:
                print(f"[LIVE] CLOSE FAILED: {response.errorMessage}")
                print(f"[LIVE] Triggering reconnect due to close failure...")
                await self._auto_reconnect()
        except Exception as e:
            print(f"[LIVE] Close ERROR: {e}")
            print(f"[LIVE] Triggering reconnect due to close exception...")
            await self._auto_reconnect()

        self._log_trade(direction, saved_entry_price, price, trade_pnl, reason)

    def _log_trade(self, direction, entry_price, exit_price, pnl, reason):
        now = datetime.now(ET)
        trade = {
            "date": now.strftime("%Y-%m-%d"),
            "entry_time": self.entry_time.strftime("%H:%M:%S") if self.entry_time else "N/A",
            "exit_time": now.strftime("%H:%M:%S"),
            "direction": direction,
            "entry": entry_price,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,
            "ema_9": self.ema_9,
            "account": os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown"),
            "session_pnl": self.live_pnl,
        }
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(trade) + "\n")
        except Exception as e:
            print(f"[BOT] Trade log write error: {e}")

    async def _shutdown(self):
        print("\n[BOT] Shutting down...")
        if self.position != 0:
            price = await self.ctx.data.get_current_price()
            if price:
                await self._flatten(price, reason="SHUTDOWN")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)

        print(f"\n[BOT] === SESSION SUMMARY ===")
        print(f"[BOT] Total P&L: ${self.live_pnl:.2f}")
        print(f"[BOT] ========================")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko 20 SMA + Ichimoku Cloud Bot")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--brick-size", type=float, default=3.0,
                        help="Renko brick size in points (default: 3.0)")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    parser.add_argument("--ntfy-topic", default="", help="ntfy.sh topic for signal relay")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []

    bot = RenkoBot(
        symbol=args.symbol,
        qty=args.qty,
        brick_size=args.brick_size,
        tg_token=args.tg_token,
        tg_chat=args.tg_chat,
        tg_keys=keys,
        ntfy_topic=args.ntfy_topic,
    )

    loop = asyncio.new_event_loop()

    def handle_signal(sig, frame):
        bot.running = False
        print("\n[BOT] Ctrl+C received, stopping...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
