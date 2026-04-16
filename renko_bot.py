"""
TopstepX Renko Strategy Bot (Shadow Mode)

Strategy: Builds virtual Renko bricks from live tick data.
Multi-timeframe: multiple brick sizes must all agree on direction to enter.
Exit: primary (smallest) brick changes direction.

Shadow Mode: Starts in SHADOW, simulating trades.
Once shadow P&L hits loss threshold (e.g. -$700), switches to LIVE.
LIVE places real trades. Once profit target hit (e.g. +$500),
goes back to SHADOW and repeats.

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python renko_bot.py --symbol NQ --qty 1 --brick-sizes "0.25,2.0,10.0"
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


def send_signals(token: str, chat_id: str, keys: list, direction: str, symbol: str, price: float, qty: int):
    for i, key in enumerate(keys):
        if i > 0:
            time.sleep(0.5)
        send_telegram(token, chat_id, f"SIGNAL|{key}|{direction}|{symbol}|{price}|{qty}")


# ============================================================
# Renko Engine
# ============================================================

class RenkoEngine:
    """Virtual Renko brick engine built from tick data.

    Traditional Renko:
    - New UP brick when price >= last_close + brick_size
    - New DOWN brick when price <= last_close - brick_size
    - Multiple bricks can form in one update if price gaps
    """

    def __init__(self, brick_size: float):
        self.brick_size = brick_size
        self.last_close = None
        self.direction = 0   # 1=up (green), -1=down (red), 0=not started
        self.brick_count = 0

    def initialize(self, price: float):
        self.last_close = round(price / self.brick_size) * self.brick_size

    def update(self, price: float) -> list:
        """Feed a tick price. Returns list of new bricks: [(open, close, direction), ...]"""
        if self.last_close is None:
            self.initialize(price)
            return []

        new_bricks = []

        while True:
            if price >= self.last_close + self.brick_size:
                new_open = self.last_close
                new_close = self.last_close + self.brick_size
                new_bricks.append((new_open, new_close, 1))
                self.last_close = new_close
                self.direction = 1
                self.brick_count += 1
            elif price <= self.last_close - self.brick_size:
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

SESSION_START = dtime(9, 44, 0)   # 9:44 AM ET
SESSION_END = dtime(16, 0)        # 4:00 PM ET

POINT_VALUE = 20.0  # NQ: $20 per point per contract


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
                 brick_sizes: list = None,
                 shadow_loss: float = 700.0, live_profit: float = 500.0,
                 tg_token: str = "", tg_chat: str = "", tg_keys: list = None):
        self.symbol = symbol
        self.qty = qty
        self.shadow_loss = shadow_loss
        self.live_profit = live_profit
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []

        # Renko engines - multiple brick sizes for multi-TF
        self.brick_sizes = brick_sizes or [0.25, 2.0, 10.0]
        self.engines = []  # populated in run()
        self.primary_idx = 0  # smallest brick size = primary trigger

        # Mode: "SHADOW" or "LIVE"
        self.mode = "SHADOW"

        # Real position state (LIVE)
        self.position = 0
        self.entry_price = 0.0

        # Shadow position state
        self.shadow_position = 0
        self.shadow_entry_price = 0.0
        self.shadow_pnl = 0.0

        # Live P&L tracking
        self.live_pnl = 0.0
        self.total_live_pnl = 0.0
        self.total_shadow_pnl = 0.0
        self.live_cycles = 0

        # Session tracking
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
        self.last_price = None

    async def run(self):
        from project_x_py import TradingSuite

        # Sort brick sizes - smallest first (primary trigger)
        self.brick_sizes.sort()

        # Create Renko engines
        self.engines = [RenkoEngine(bs) for bs in self.brick_sizes]

        print(f"[BOT] Renko Strategy Bot - SHADOW MODE")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Brick sizes: {', '.join(f'{bs}' for bs in self.brick_sizes)}")
        print(f"[BOT] Primary (trigger): {self.brick_sizes[0]} | Confirmations: {', '.join(f'{bs}' for bs in self.brick_sizes[1:])}")
        print(f"[BOT] Shadow loss: -${self.shadow_loss:.0f} | Live profit: +${self.live_profit:.0f}")
        print(f"[BOT] Session: {SESSION_START.strftime('%H:%M')} - {SESSION_END.strftime('%H:%M')} ET")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        print()

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["1min"],
            initial_days=0,
        )

        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        # Initialize engines with current price
        price = await self.ctx.data.get_current_price()
        if price:
            for eng in self.engines:
                eng.initialize(price)
            self.last_price = price
            print(f"[BOT] Renko initialized at {price:.2f}")
            for eng in self.engines:
                print(f"  Brick {eng.brick_size}: ref={eng.last_close:.2f}")
        print()

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Starting in SHADOW mode")
        print(f"[BOT] Press Ctrl+C to stop")
        print()

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(0.25)  # Poll 4x/sec for tick-level detection
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

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

        # Skip if price hasn't changed
        if price == self.last_price:
            return
        self.last_price = price

        # Feed tick to ALL Renko engines
        all_new_bricks = []
        for eng in self.engines:
            bricks = eng.update(price)
            all_new_bricks.append(bricks)

        # Log new bricks
        for i, bricks in enumerate(all_new_bricks):
            for brick in bricks:
                b_open, b_close, b_dir = brick
                color = "GREEN" if b_dir == 1 else "RED"
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] [RENKO {self.brick_sizes[i]}] New {color} brick: {b_open:.2f} -> {b_close:.2f} (#{self.engines[i].brick_count})")

        # Check session
        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        if sess_ended:
            if self.position != 0:
                print(f"[SESSION] Session ended - flattening LIVE position")
                await self._flatten(price, reason="SESSION_END")
            if self.shadow_position != 0:
                self._shadow_flatten(price, reason="SESSION_END")
            self.was_in_session = currently_in_session
            return

        sess_started = not self.was_in_session and currently_in_session
        if sess_started:
            self.mode = "SHADOW"
            self.shadow_pnl = 0.0
            self.shadow_position = 0
            self.live_pnl = 0.0
            self.total_live_pnl = 0.0
            self.total_shadow_pnl = 0.0
            self.live_cycles = 0
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SESSION] New session started - SHADOW mode")

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # Only act when the primary (smallest) brick engine forms a new brick
        primary_bricks = all_new_bricks[self.primary_idx]
        if not primary_bricks:
            return

        # Get direction from each engine
        directions = [eng.direction for eng in self.engines]

        # Multi-TF confirmation: all engines must agree on direction
        all_up = all(d == 1 for d in directions)
        all_down = all(d == -1 for d in directions)

        # Primary brick's latest direction (the trigger)
        primary_dir = primary_bricks[-1][2]

        now = datetime.now(ET).strftime("%H:%M:%S")
        dir_str = " | ".join(f"{self.brick_sizes[i]}={'UP' if d==1 else 'DN' if d==-1 else '??'}" for i, d in enumerate(directions))
        print(f"[{now}] [MTF] {dir_str} | AllUp={all_up} AllDn={all_down}")

        if self.mode == "SHADOW":
            self._shadow_logic(price, all_up, all_down, primary_dir)
        else:
            await self._live_logic(price, all_up, all_down, primary_dir)

    # ==========================================================
    # SHADOW MODE
    # ==========================================================

    def _shadow_logic(self, price: float, all_up: bool, all_down: bool, primary_dir: int):
        # Check threshold
        if self.shadow_position != 0:
            unrealized = (price - self.shadow_entry_price) * self.shadow_position * POINT_VALUE * self.qty
            total = self.shadow_pnl + unrealized
            if total <= -self.shadow_loss:
                self._shadow_flatten(price, reason="SWITCHING_TO_LIVE")
                self.total_shadow_pnl += self.shadow_pnl
                self._switch_to_live()
                return
        elif self.shadow_pnl <= -self.shadow_loss:
            self.total_shadow_pnl += self.shadow_pnl
            self._switch_to_live()
            return

        # Entry: all timeframes agree
        if all_up and self.shadow_position != 1:
            if self.shadow_position == -1:
                self._shadow_flatten(price, reason="RENKO_UP")
            if self.shadow_position == 0:
                self._shadow_enter(price, 1, "LONG")

        elif all_down and self.shadow_position != -1:
            if self.shadow_position == 1:
                self._shadow_flatten(price, reason="RENKO_DOWN")
            if self.shadow_position == 0:
                self._shadow_enter(price, -1, "SHORT")

        # Exit: primary brick changes against position (even if larger TFs disagree)
        elif self.shadow_position == 1 and primary_dir == -1:
            self._shadow_flatten(price, reason="PRIMARY_REVERSAL")

        elif self.shadow_position == -1 and primary_dir == 1:
            self._shadow_flatten(price, reason="PRIMARY_REVERSAL")

    def _shadow_enter(self, price: float, direction: int, label: str):
        self.shadow_position = direction
        self.shadow_entry_price = price
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [SHADOW] >>> {label} @ {price:.2f} | Shadow P&L: ${self.shadow_pnl:.2f}")

    def _shadow_flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.shadow_position == 1 else "SHORT"
        trade_pnl = (price - self.shadow_entry_price) * self.shadow_position * POINT_VALUE * self.qty
        self.shadow_pnl += trade_pnl
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [SHADOW] <<< EXIT {direction} @ {price:.2f} | Trade: ${trade_pnl:+.2f} | Shadow P&L: ${self.shadow_pnl:.2f} | {reason}")
        self.shadow_position = 0
        self.shadow_entry_price = 0.0

    # ==========================================================
    # LIVE MODE
    # ==========================================================

    async def _live_logic(self, price: float, all_up: bool, all_down: bool, primary_dir: int):
        # Check profit target
        if self.position != 0:
            unrealized = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
            total = self.live_pnl + unrealized

            if total >= self.live_profit:
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"\n[{now}] [LIVE] *** PROFIT TARGET HIT! Live P&L: ${total:.2f} ***")
                await self._flatten(price, reason="PROFIT_TARGET")
                self.total_live_pnl += self.live_pnl
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0)
                self._switch_to_shadow()
                return

        # Entry: all timeframes agree
        if all_up and self.position != 1:
            if self.position == -1:
                await self._flatten(price, reason="RENKO_UP")
            if self.position == 0:
                await self._enter_long(price)

        elif all_down and self.position != -1:
            if self.position == 1:
                await self._flatten(price, reason="RENKO_DOWN")
            if self.position == 0:
                await self._enter_short(price)

        # Exit: primary brick reversal
        elif self.position == 1 and primary_dir == -1:
            await self._flatten(price, reason="PRIMARY_REVERSAL")

        elif self.position == -1 and primary_dir == 1:
            await self._flatten(price, reason="PRIMARY_REVERSAL")

    async def _enter_long(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [LIVE] >>> ENTERING LONG @ {price:.2f} | Live P&L: ${self.live_pnl:.2f} | Target: +${self.live_profit:.0f}")
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
                             "LONG", self.symbol, price, self.qty)
            else:
                print(f"[LIVE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[LIVE] Order ERROR: {e}")

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [LIVE] >>> ENTERING SHORT @ {price:.2f} | Live P&L: ${self.live_pnl:.2f} | Target: +${self.live_profit:.0f}")
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
                             "SHORT", self.symbol, price, self.qty)
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
    # Mode switching
    # ==========================================================

    def _switch_to_live(self):
        self.mode = "LIVE"
        self.live_pnl = 0.0
        self.live_cycles += 1
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [MODE] ========================================")
        print(f"[{now}] [MODE] >>> SWITCHING TO LIVE (cycle #{self.live_cycles})")
        print(f"[{now}] [MODE] Shadow P&L was: ${self.shadow_pnl:.2f}")
        print(f"[{now}] [MODE] ========================================\n")

    def _switch_to_shadow(self):
        self.mode = "SHADOW"
        self.shadow_pnl = 0.0
        self.shadow_position = 0
        self.shadow_entry_price = 0.0
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [MODE] ========================================")
        print(f"[{now}] [MODE] >>> SWITCHING TO SHADOW")
        print(f"[{now}] [MODE] Live P&L was: ${self.live_pnl:.2f} (total: ${self.total_live_pnl:.2f})")
        print(f"[{now}] [MODE] ========================================\n")

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
                             "FLAT", self.symbol, price, 0)

        if self.shadow_position != 0:
            price = await self.ctx.data.get_current_price()
            if price:
                self._shadow_flatten(price, reason="SHUTDOWN")

        self.total_live_pnl += self.live_pnl
        self.total_shadow_pnl += self.shadow_pnl

        print(f"\n[BOT] === SESSION SUMMARY ===")
        print(f"[BOT] Live cycles: {self.live_cycles}")
        print(f"[BOT] Total LIVE P&L: ${self.total_live_pnl:.2f}")
        print(f"[BOT] Total SHADOW P&L: ${self.total_shadow_pnl:.2f} (not real)")
        print(f"[BOT] ========================")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko Bot (Shadow Mode)")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--brick-sizes", default="0.25,2.0,10.0",
                        help="Comma-separated Renko brick sizes (smallest=trigger, rest=confirmation)")
    parser.add_argument("--shadow-loss", type=float, default=700.0,
                        help="Shadow P&L loss threshold to switch to LIVE")
    parser.add_argument("--live-profit", type=float, default=500.0,
                        help="Live P&L profit target to switch back to SHADOW")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    args = parser.parse_args()

    brick_sizes = [float(x.strip()) for x in args.brick_sizes.split(",") if x.strip()]
    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []

    bot = RenkoBot(
        symbol=args.symbol,
        qty=args.qty,
        brick_sizes=brick_sizes,
        shadow_loss=args.shadow_loss,
        live_profit=args.live_profit,
        tg_token=args.tg_token,
        tg_chat=args.tg_chat,
        tg_keys=keys,
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
