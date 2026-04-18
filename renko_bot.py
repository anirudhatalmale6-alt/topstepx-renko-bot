"""
TopstepX Renko Multi-Timeframe Strategy Bot (LIVE)

Strategy: Traditional Renko 0.25 bricks built from candle CLOSE prices.
Multi-timeframe: builds separate Renko from 1min, 3min, 5min, 15min closes.
- ENTRY: all 4 timeframes must show same Renko direction (all BULLISH = LONG)
- EXIT: any timeframe closes a brick in opposite direction = immediate exit
- $500 profit target per session cycle

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
# Telegram helper
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
                time.sleep(2 * (attempt + 1))
                continue
            print(f"[TG] Send failed: {e}")
            return
        except Exception as e:
            print(f"[TG] Send failed: {e}")
            return


def send_ntfy(topic: str, message: str):
    """Send signal to ntfy.sh relay for copier bots to read."""
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[NTFY] Send failed: {e}")


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
    - New UP brick: close >= last_brick_close + brick_size
    - New DOWN brick: close <= last_brick_close - brick_size
    - Multiple bricks can form if price gaps past multiple levels.
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

SESSION_START = dtime(11, 0, 0)    # 11:00 AM ET
SESSION_END = dtime(16, 0)         # 4:00 PM ET

POINT_VALUE = 20.0  # NQ: $20 per point per contract

# Trading days (0=Monday, 1=Tuesday, ..., 4=Friday)
TRADING_DAYS = [0, 1, 2]  # Mon, Tue, Wed only

# Renko timeframes (in minutes) - all use same brick size
TF_MINUTES = [1, 3, 5, 15]

# Sub-minute Renko timeframes (in seconds) - fed by SDK's native sub-minute bars
TF_SECONDS = [15]  # 15-second Renko as 5th TF

# Combined label for all timeframes
ALL_TF_LABELS = ["15sec", "1min", "3min", "5min", "15min"]


def in_session() -> bool:
    now = datetime.now(ET)
    # Check day of week first
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
                 live_profit: float = 500.0,
                 tg_token: str = "", tg_chat: str = "", tg_keys: list = None,
                 ntfy_topic: str = ""):
        self.symbol = symbol
        self.qty = qty
        self.brick_size = brick_size
        self.live_profit = live_profit
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []
        self.ntfy_topic = ntfy_topic

        # Renko engines - one per timeframe, same brick size
        self.engines = {}       # keyed by minute TF: {1: engine, 3: engine, ...}
        self.tf_labels = {}
        self.sec_engines = {}   # keyed by second TF: {15: engine}
        self.sec_tf_labels = {}

        # Bar counting for 3min/5min derivation
        self.bar_count = 0
        self.last_1min_time = None
        self.last_15sec_time = None

        # Real position
        self.position = 0       # 1=long, -1=short, 0=flat
        self.entry_price = 0.0
        self.entry_time = None   # datetime of entry

        # Trade log file
        self.trade_log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "trade_log.jsonl"
        )

        # P&L tracking
        self.live_pnl = 0.0
        self.total_live_pnl = 0.0
        self.session_done = False  # True after hitting profit target

        # Session
        self.was_in_session = False

        # Connection health
        self.last_price_time = None
        self.connection_alive = True
        self.disconnect_alert_sent = False
        self.STALE_THRESHOLD = 60

        # SDK
        self.suite = None
        self.ctx = None
        self.running = False

    async def run(self):
        from project_x_py import TradingSuite

        for tf in TF_MINUTES:
            label = f"{tf}min"
            self.engines[tf] = RenkoEngine(self.brick_size, label)
            self.tf_labels[tf] = label

        for tf_s in TF_SECONDS:
            label = f"{tf_s}sec"
            self.sec_engines[tf_s] = RenkoEngine(self.brick_size, label)
            self.sec_tf_labels[tf_s] = label

        total_tfs = len(TF_MINUTES) + len(TF_SECONDS)
        print(f"[BOT] Renko Multi-TF Strategy Bot - LIVE MODE")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Brick size: {self.brick_size} (Traditional)")
        print(f"[BOT] Timeframes: {', '.join(ALL_TF_LABELS)}")
        print(f"[BOT] ENTRY: all {total_tfs} timeframes ALIGNED")
        print(f"[BOT] EXIT: any timeframe MISALIGNS (brick close)")
        print(f"[BOT] No TP / No Shadow - pure alignment trading")
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
        trading_day_str = ", ".join(day_names[d] for d in TRADING_DAYS)
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - {SESSION_END.strftime('%H:%M')} ET ({trading_day_str} only)")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        print()

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["15sec", "1min", "15min"],
            initial_days=1,
        )
        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        price = await self.ctx.data.get_current_price()
        if price:
            for tf in TF_MINUTES:
                self.engines[tf].initialize(price)
            for tf_s in TF_SECONDS:
                self.sec_engines[tf_s].initialize(price)
            print(f"[BOT] Renko engines initialized at {price:.2f}")

        await self._seed_history()

        print()
        self.running = True
        self.was_in_session = in_session()

        # Show current alignment
        self._print_alignment()

        print(f"\n[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Trading LIVE - no shadow mode")
        print(f"[BOT] Press Ctrl+C to stop")
        print()

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _seed_history(self):
        """Feed historical candles to warm up Renko engines."""
        # Seed minute-based engines from 1min history
        data = await self.ctx.data.get_data("1min", bars=200)
        if data is None or len(data) == 0:
            print("[BOT] No historical data for seeding")
            return

        rows = list(data.iter_rows(named=True))
        print(f"[BOT] Seeding with {len(rows)} historical 1min bars...")

        for i, row in enumerate(rows):
            close = float(row["close"])
            self.engines[1].feed_close(close)
            if (i + 1) % 3 == 0:
                self.engines[3].feed_close(close)
            if (i + 1) % 5 == 0:
                self.engines[5].feed_close(close)
            if (i + 1) % 15 == 0:
                self.engines[15].feed_close(close)

        for tf in TF_MINUTES:
            eng = self.engines[tf]
            dir_str = "BULLISH" if eng.direction == 1 else "BEARISH" if eng.direction == -1 else "NONE"
            print(f"  {self.tf_labels[tf]}: {eng.brick_count} bricks, {dir_str}, ref={eng.last_close:.2f}")

        self.bar_count = len(rows)

        # Seed 15-second engine from 15sec history
        try:
            data_15s = await self.ctx.data.get_data("15sec", bars=800)
            if data_15s is not None and len(data_15s) > 0:
                rows_15s = list(data_15s.iter_rows(named=True))
                print(f"[BOT] Seeding with {len(rows_15s)} historical 15sec bars...")
                for row in rows_15s:
                    close = float(row["close"])
                    self.sec_engines[15].feed_close(close)
                eng = self.sec_engines[15]
                dir_str = "BULLISH" if eng.direction == 1 else "BEARISH" if eng.direction == -1 else "NONE"
                print(f"  15sec: {eng.brick_count} bricks, {dir_str}, ref={eng.last_close:.2f}")
            else:
                print("[BOT] No 15sec historical data - engine will warm up from live data")
        except Exception as e:
            print(f"[BOT] Could not seed 15sec history: {e} - will warm up from live data")

    def _get_all_directions(self):
        """Get direction dict for all engines (sub-minute + minute)."""
        directions = {}
        for tf_s in TF_SECONDS:
            directions[self.sec_tf_labels[tf_s]] = self.sec_engines[tf_s].direction
        for tf in TF_MINUTES:
            directions[self.tf_labels[tf]] = self.engines[tf].direction
        return directions

    def _print_alignment(self):
        """Print TradingView-style alignment table."""
        print(f"\n  {'Timeframe':<12} {'Direction':<12}")
        print(f"  {'-'*24}")

        for tf_s in TF_SECONDS:
            eng = self.sec_engines[tf_s]
            d = "BULLISH" if eng.direction == 1 else "BEARISH" if eng.direction == -1 else "NONE"
            print(f"  {self.sec_tf_labels[tf_s]:<12} {d:<12}")

        for tf in TF_MINUTES:
            eng = self.engines[tf]
            d = "BULLISH" if eng.direction == 1 else "BEARISH" if eng.direction == -1 else "NONE"
            print(f"  {self.tf_labels[tf]:<12} {d:<12}")

        directions = self._get_all_directions()
        all_up = all(d == 1 for d in directions.values())
        all_down = all(d == -1 for d in directions.values())
        aligned = all_up or all_down

        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"

        print(f"  {'ALIGNMENT':<12} {'ALIGNED' if aligned else 'NOT ALIGNED':<12}")
        print(f"  {'POSITION':<12} {pos_str:<12}")

    async def _tick(self):
        # If disconnected (between sessions), skip until reconnect
        if self.ctx is None:
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
                    msg = f"DISCONNECTED - No price data for {int(elapsed)}s ({now} ET)"
                    print(f"[{now}] [ALERT] {msg}")
                    send_telegram(self.tg_token, self.tg_chat, f"STATUS|{msg}")
            return

        self.last_price_time = now_ts
        if not self.connection_alive:
            self.connection_alive = True
            self.disconnect_alert_sent = False
            now = datetime.now(ET).strftime("%H:%M:%S")
            msg = f"RECONNECTED - Price data restored ({now} ET)"
            print(f"[{now}] [ALERT] {msg}")
            send_telegram(self.tg_token, self.tg_chat, f"STATUS|{msg}")

        # Session boundaries
        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        if sess_ended:
            if self.position != 0:
                print(f"[SESSION] Session ended - flattening position")
                await self._flatten(price, reason="SESSION_END")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)
            # Disconnect to avoid stale WebSocket overnight
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
            self.total_live_pnl = 0.0
            self.session_done = False
            self.bar_count = 0
            # Reconnect if disconnected
            if self.suite is None:
                from project_x_py import TradingSuite
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now_str}] [SESSION] Reconnecting to TopstepX...")
                self.suite = await TradingSuite.create(
                    instruments=self.symbol,
                    timeframes=["1min", "15min"],
                    initial_days=1,
                )
                self.ctx = self.suite[self.symbol]
                print(f"[{now_str}] [SESSION] Connected fresh")
                # Re-seed Renko engines with latest data
                await self._seed_history()
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] New session started - LIVE mode")
            self._print_alignment()

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # Check for new 15-second bar close
        new_renko_event = False
        now = datetime.now(ET).strftime("%H:%M:%S")

        try:
            data_15s = await self.ctx.data.get_data("15sec", bars=1)
            if data_15s is not None and len(data_15s) > 0:
                rows_15s = list(data_15s.iter_rows(named=True))
                last_15s = rows_15s[-1]
                bar_time_15s = last_15s.get("timestamp") or last_15s.get("time")

                if bar_time_15s != self.last_15sec_time:
                    self.last_15sec_time = bar_time_15s
                    close_15s = float(last_15s["close"])

                    bricks = self.sec_engines[15].feed_close(close_15s)
                    if bricks:
                        new_renko_event = True
                        for b in bricks:
                            color = "BULLISH" if b[2] == 1 else "BEARISH"
                            print(f"[{now}] [RENKO 15sec] {color} brick #{self.sec_engines[15].brick_count}: {b[0]:.2f} -> {b[1]:.2f}")
        except Exception as e:
            pass  # 15sec data might not be available yet during warmup

        # Check for new 1-minute bar close
        data_1m = await self.ctx.data.get_data("1min", bars=1)
        if data_1m is not None and len(data_1m) > 0:
            rows = list(data_1m.iter_rows(named=True))
            last_row = rows[-1]
            bar_time = last_row.get("timestamp") or last_row.get("time")

            if bar_time != self.last_1min_time:
                self.last_1min_time = bar_time
                self.bar_count += 1
                close_1m = float(last_row["close"])

                # Feed 1min Renko (every bar)
                bricks = self.engines[1].feed_close(close_1m)
                if bricks:
                    new_renko_event = True
                    for b in bricks:
                        color = "BULLISH" if b[2] == 1 else "BEARISH"
                        print(f"[{now}] [RENKO 1min] {color} brick #{self.engines[1].brick_count}: {b[0]:.2f} -> {b[1]:.2f}")

                # Feed 3min Renko (every 3rd bar)
                if self.bar_count % 3 == 0:
                    bricks = self.engines[3].feed_close(close_1m)
                    if bricks:
                        new_renko_event = True
                        for b in bricks:
                            color = "BULLISH" if b[2] == 1 else "BEARISH"
                            print(f"[{now}] [RENKO 3min] {color} brick #{self.engines[3].brick_count}: {b[0]:.2f} -> {b[1]:.2f}")

                # Feed 5min Renko (every 5th bar)
                if self.bar_count % 5 == 0:
                    bricks = self.engines[5].feed_close(close_1m)
                    if bricks:
                        new_renko_event = True
                        for b in bricks:
                            color = "BULLISH" if b[2] == 1 else "BEARISH"
                            print(f"[{now}] [RENKO 5min] {color} brick #{self.engines[5].brick_count}: {b[0]:.2f} -> {b[1]:.2f}")

                # Feed 15min Renko (every 15th bar)
                if self.bar_count % 15 == 0:
                    bricks = self.engines[15].feed_close(close_1m)
                    if bricks:
                        new_renko_event = True
                        for b in bricks:
                            color = "BULLISH" if b[2] == 1 else "BEARISH"
                            print(f"[{now}] [RENKO 15min] {color} brick #{self.engines[15].brick_count}: {b[0]:.2f} -> {b[1]:.2f}")

        if not new_renko_event:
            return

        # Get alignment status across ALL timeframes (15sec + 1min + 3min + 5min + 15min)
        directions = self._get_all_directions()
        all_up = all(d == 1 for d in directions.values())
        all_down = all(d == -1 for d in directions.values())
        aligned = all_up or all_down

        # Check misalignment against current position
        misaligned = False
        if self.position == 1:
            misaligned = any(d == -1 for d in directions.values())
        elif self.position == -1:
            misaligned = any(d == 1 for d in directions.values())

        dir_str = " | ".join(f"{label}={'BULL' if d==1 else 'BEAR' if d==-1 else '??'}" for label, d in directions.items())
        align_str = "ALIGNED" if aligned else "NOT ALIGNED"
        print(f"[{now}] [MTF] {dir_str} | {align_str}")

        await self._live_logic(price, all_up, all_down, misaligned)

    # ==========================================================
    # LIVE TRADING LOGIC
    # ==========================================================

    async def _live_logic(self, price: float, all_up: bool, all_down: bool, misaligned: bool):
        # Exit on misalignment
        if self.position != 0 and misaligned:
            await self._flatten(price, reason="MISALIGNED")
            send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                         "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)

        # Entry when all aligned
        if all_up and self.position <= 0:
            if self.position == -1:
                await self._flatten(price, reason="FLIP_LONG")
            if self.position == 0:
                await self._enter_long(price)

        elif all_down and self.position >= 0:
            if self.position == 1:
                await self._flatten(price, reason="FLIP_SHORT")
            if self.position == 0:
                await self._enter_short(price)

    async def _enter_long(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [LIVE] >>> ENTERING LONG @ {price:.2f} | Live P&L: ${self.live_pnl:.2f}")
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
                print(f"[LIVE] Order filled. ID: {response.orderId}")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "LONG", self.symbol, price, self.qty, ntfy_topic=self.ntfy_topic)
            else:
                print(f"[LIVE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[LIVE] Order ERROR: {e}")

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [LIVE] >>> ENTERING SHORT @ {price:.2f} | Live P&L: ${self.live_pnl:.2f}")
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
                print(f"[LIVE] Order filled. ID: {response.orderId}")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "SHORT", self.symbol, price, self.qty, ntfy_topic=self.ntfy_topic)
            else:
                print(f"[LIVE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[LIVE] Order ERROR: {e}")

    async def _flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        trade_pnl = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
        self.live_pnl += trade_pnl

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [LIVE] <<< EXITING {direction} @ {price:.2f} | Trade: ${trade_pnl:+.2f} | Live P&L: ${self.live_pnl:.2f} | {reason}")

        try:
            close_side = 1 if self.position == 1 else 0
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=close_side,
                size=self.qty,
            )
            if response.success:
                print(f"[LIVE] Position closed. ID: {response.orderId}")
            else:
                print(f"[LIVE] CLOSE FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[LIVE] Close ERROR: {e}")

        # Log trade to file
        self._log_trade(direction, self.entry_price, price, trade_pnl, reason)

        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None

    def _log_trade(self, direction, entry_price, exit_price, pnl, reason):
        """Append trade to JSONL log file for long-term data collection."""
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
            "account": os.environ.get("PROJECT_X_ACCOUNT_NAME", "unknown"),
            "session_pnl": self.live_pnl,
        }
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(trade) + "\n")
        except Exception as e:
            print(f"[BOT] Trade log write error: {e}")

    # ==========================================================
    # Shutdown
    # ==========================================================

    async def _shutdown(self):
        print("\n[BOT] Shutting down...")
        if self.position != 0:
            price = await self.ctx.data.get_current_price()
            if price:
                await self._flatten(price, reason="SHUTDOWN")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0, ntfy_topic=self.ntfy_topic)

        self.total_live_pnl += self.live_pnl

        print(f"\n[BOT] === SESSION SUMMARY ===")
        print(f"[BOT] Total P&L: ${self.total_live_pnl:.2f}")
        print(f"[BOT] ========================")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko Multi-TF Bot (LIVE)")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--brick-size", type=float, default=0.25,
                        help="Renko brick size in points (default: 0.25)")
    parser.add_argument("--shadow-loss", type=float, default=700.0,
                        help="(ignored, kept for backwards compat)")
    parser.add_argument("--live-profit", type=float, default=500.0,
                        help="Profit target to stop trading for the session")
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
        live_profit=args.live_profit,
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
