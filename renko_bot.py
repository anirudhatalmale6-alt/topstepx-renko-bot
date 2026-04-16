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

# Renko timeframes (in minutes) - all use same brick size
TF_MINUTES = [1, 3, 5, 15]


def in_session() -> bool:
    now = datetime.now(ET).time()
    if SESSION_START > SESSION_END:
        return now >= SESSION_START or now < SESSION_END
    return SESSION_START <= now < SESSION_END


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
        self.engines = {}
        self.tf_labels = {}

        # Bar counting for 3min/5min derivation
        self.bar_count = 0
        self.last_1min_time = None

        # Real position
        self.position = 0       # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

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

        print(f"[BOT] Renko Multi-TF Strategy Bot - LIVE MODE")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Brick size: {self.brick_size} (Traditional)")
        print(f"[BOT] Timeframes: {', '.join(self.tf_labels[tf] for tf in TF_MINUTES)}")
        print(f"[BOT] ENTRY: all 4 timeframes ALIGNED")
        print(f"[BOT] EXIT: any timeframe MISALIGNS (brick close)")
        print(f"[BOT] No TP / No Shadow - pure alignment trading")
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - {SESSION_END.strftime('%H:%M')} ET")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        print()

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["1min", "15min"],
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
        """Feed historical 1min candles to warm up Renko engines."""
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

    def _print_alignment(self):
        """Print TradingView-style alignment table."""
        print(f"\n  {'Timeframe':<12} {'Direction':<12}")
        print(f"  {'-'*24}")
        for tf in TF_MINUTES:
            eng = self.engines[tf]
            d = "BULLISH" if eng.direction == 1 else "BEARISH" if eng.direction == -1 else "NONE"
            print(f"  {self.tf_labels[tf]:<12} {d:<12}")

        directions = [self.engines[tf].direction for tf in TF_MINUTES]
        all_up = all(d == 1 for d in directions)
        all_down = all(d == -1 for d in directions)
        aligned = all_up or all_down

        pos_str = "LONG" if self.position == 1 else "SHORT" if self.position == -1 else "FLAT"

        print(f"  {'ALIGNMENT':<12} {'ALIGNED' if aligned else 'NOT ALIGNED':<12}")
        print(f"  {'POSITION':<12} {pos_str:<12}")

    async def _tick(self):
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
            self.was_in_session = currently_in_session
            return

        sess_started = not self.was_in_session and currently_in_session
        if sess_started:
            self.live_pnl = 0.0
            self.total_live_pnl = 0.0
            self.session_done = False
            self.bar_count = 0
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] New session started - LIVE mode")
            self._print_alignment()

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # Check for new 1-minute bar close
        new_renko_event = False
        data_1m = await self.ctx.data.get_data("1min", bars=1)
        if data_1m is not None and len(data_1m) > 0:
            rows = list(data_1m.iter_rows(named=True))
            last_row = rows[-1]
            bar_time = last_row.get("timestamp") or last_row.get("time")

            if bar_time != self.last_1min_time:
                self.last_1min_time = bar_time
                self.bar_count += 1
                close_1m = float(last_row["close"])
                now = datetime.now(ET).strftime("%H:%M:%S")

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

        # Get alignment status
        directions = {tf: self.engines[tf].direction for tf in TF_MINUTES}
        all_up = all(d == 1 for d in directions.values())
        all_down = all(d == -1 for d in directions.values())
        aligned = all_up or all_down

        # Check misalignment against current position
        misaligned = False
        if self.position == 1:
            misaligned = any(d == -1 for d in directions.values())
        elif self.position == -1:
            misaligned = any(d == 1 for d in directions.values())

        now = datetime.now(ET).strftime("%H:%M:%S")
        dir_str = " | ".join(f"{self.tf_labels[tf]}={'BULL' if d==1 else 'BEAR' if d==-1 else '??'}" for tf, d in directions.items())
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

        self.position = 0
        self.entry_price = 0.0

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
