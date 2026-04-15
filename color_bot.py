"""
TopstepX Realtime Candle Color Strategy Bot (Shadow Mode)

Strategy: Trades based on the current 15-minute candle color.
- Green candle (close > open) = LONG
- Red candle (close < open) = SHORT

Shadow Mode: Bot starts in SHADOW mode, simulating trades without real orders.
Once shadow P&L hits the loss threshold (e.g. -$700), switches to LIVE mode.
LIVE mode places real trades. Once real P&L hits profit target (e.g. +$500),
goes back to SHADOW and repeats.

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python color_bot.py --symbol NQ --qty 1 --shadow-loss 700 --live-profit 500
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
    """Send a message to Telegram channel with retry on rate limit."""
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
    """Send a signal for each key with spacing to avoid rate limits."""
    for i, key in enumerate(keys):
        if i > 0:
            time.sleep(0.5)
        send_telegram(token, chat_id, f"SIGNAL|{key}|{direction}|{symbol}|{price}|{qty}")


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(9, 44, 0)  # 9:44 ET
SESSION_END = dtime(16, 0)

TIMEFRAME = "15min"

# NQ: 1 point = $20 per contract
POINT_VALUE = 20.0


# ============================================================
# Session helpers
# ============================================================

def in_session() -> bool:
    now = datetime.now(ET).time()
    return SESSION_START <= now < SESSION_END


# ============================================================
# Main Bot
# ============================================================

class CandleColorBot:
    def __init__(self, symbol: str, qty: int = 1,
                 shadow_loss: float = 700.0, live_profit: float = 500.0,
                 tg_token: str = "", tg_chat: str = "", tg_keys: list = None):
        self.symbol = symbol
        self.qty = qty
        self.shadow_loss = shadow_loss    # Shadow P&L threshold to go LIVE
        self.live_profit = live_profit    # Live P&L target to go back to SHADOW
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []

        # Mode: "SHADOW" or "LIVE"
        self.mode = "SHADOW"

        # Real position state (LIVE mode only)
        self.position = 0       # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

        # Shadow position state (SHADOW mode only)
        self.shadow_position = 0
        self.shadow_entry_price = 0.0
        self.shadow_pnl = 0.0

        # Live P&L tracking (resets each time we enter LIVE)
        self.live_pnl = 0.0

        # Total session stats
        self.total_live_pnl = 0.0
        self.total_shadow_pnl = 0.0
        self.live_cycles = 0

        # Candle tracking
        self.current_candle_open = None
        self.last_candle_time = None

        # Opening flip logic - wait for first color change before entering
        self.waiting_for_flip = True
        self.initial_color = None

        # Session tracking
        self.was_in_session = False

        # SDK objects
        self.suite = None
        self.ctx = None
        self.running = False

    async def run(self):
        from project_x_py import TradingSuite

        print(f"[BOT] Candle Color Bot - SHADOW MODE")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Timeframe: {TIMEFRAME}")
        print(f"[BOT] Shadow loss trigger: -${self.shadow_loss:.0f}")
        print(f"[BOT] Live profit target: +${self.live_profit:.0f}")
        print(f"[BOT] Session: 09:44 - 16:00 ET")
        if self.tg_token and self.tg_chat and self.tg_keys:
            print(f"[BOT] Telegram signals: ENABLED ({len(self.tg_keys)} keys)")
        else:
            print(f"[BOT] Telegram signals: OFF")
        print()

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=[TIMEFRAME],
            initial_days=1,
        )

        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        await self._init_candle()

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Starting in SHADOW mode")
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

    async def _init_candle(self):
        data = await self.ctx.data.get_data(TIMEFRAME, bars=1)
        if data is not None and len(data) > 0:
            rows = list(data.iter_rows(named=True))
            last_row = rows[-1]
            self.current_candle_open = float(last_row["open"])
            self.last_candle_time = last_row.get("timestamp") or last_row.get("time")
            print(f"[BOT] Current {TIMEFRAME} candle open: {self.current_candle_open:.2f}")
        else:
            print("[BOT] No candle data available!")

    def _switch_to_live(self):
        """Switch from SHADOW to LIVE mode."""
        self.mode = "LIVE"
        self.live_pnl = 0.0
        self.live_cycles += 1
        self.waiting_for_flip = True
        self.initial_color = None
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [MODE] ========================================")
        print(f"[{now}] [MODE] >>> SWITCHING TO LIVE (cycle #{self.live_cycles})")
        print(f"[{now}] [MODE] Shadow P&L was: ${self.shadow_pnl:.2f}")
        print(f"[{now}] [MODE] Waiting for color flip before first trade...")
        print(f"[{now}] [MODE] ========================================\n")

    def _switch_to_shadow(self):
        """Switch from LIVE to SHADOW mode."""
        self.mode = "SHADOW"
        self.shadow_pnl = 0.0
        self.shadow_position = 0
        self.shadow_entry_price = 0.0
        self.waiting_for_flip = True
        self.initial_color = None
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [MODE] ========================================")
        print(f"[{now}] [MODE] >>> SWITCHING TO SHADOW")
        print(f"[{now}] [MODE] Live P&L was: ${self.live_pnl:.2f} (total live: ${self.total_live_pnl:.2f})")
        print(f"[{now}] [MODE] Waiting for -${self.shadow_loss:.0f} before going live again...")
        print(f"[{now}] [MODE] ========================================\n")

    async def _tick(self):
        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        # Check for new candle
        data = await self.ctx.data.get_data(TIMEFRAME, bars=1)
        if data is not None and len(data) > 0:
            rows = list(data.iter_rows(named=True))
            last_row = rows[-1]
            candle_time = last_row.get("timestamp") or last_row.get("time")
            candle_open = float(last_row["open"])

            if candle_time != self.last_candle_time:
                self.current_candle_open = candle_open
                self.last_candle_time = candle_time
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] New {TIMEFRAME} candle opened @ {candle_open:.2f}")

        if self.current_candle_open is None:
            return

        # Calculate candle color
        body = abs(price - self.current_candle_open)
        is_doji = body < 0.01
        is_green = price > self.current_candle_open and not is_doji
        is_red = price < self.current_candle_open and not is_doji

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

        # Reset at session start
        sess_started = not self.was_in_session and currently_in_session
        if sess_started:
            self.mode = "SHADOW"
            self.shadow_pnl = 0.0
            self.shadow_position = 0
            self.live_pnl = 0.0
            self.total_live_pnl = 0.0
            self.total_shadow_pnl = 0.0
            self.live_cycles = 0
            self.waiting_for_flip = True
            self.initial_color = None
            print(f"[SESSION] New session started - SHADOW mode, waiting for color flip")

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # Flip logic: wait for first color change before entering/simulating
        if self.waiting_for_flip:
            if is_green or is_red:
                current_color = "GREEN" if is_green else "RED"
                if self.initial_color is None:
                    self.initial_color = current_color
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.mode}] [FLIP] Initial color: {current_color} - waiting for flip...")
                    return
                elif current_color != self.initial_color:
                    self.waiting_for_flip = False
                    now = datetime.now(ET).strftime("%H:%M:%S")
                    print(f"[{now}] [{self.mode}] [FLIP] Color flipped from {self.initial_color} to {current_color} - TRADING ENABLED")
                else:
                    return
            else:
                return

        # Route to shadow or live logic
        if self.mode == "SHADOW":
            self._shadow_tick(price, is_green, is_red)
        else:
            await self._live_tick(price, is_green, is_red)

    # ==========================================================
    # SHADOW MODE - simulated trades, no real orders
    # ==========================================================

    def _shadow_tick(self, price: float, is_green: bool, is_red: bool):
        # Check if shadow should go LIVE
        if self.shadow_pnl <= -self.shadow_loss:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"[{now}] [SHADOW] Shadow P&L hit -${self.shadow_loss:.0f} threshold!")
            if self.shadow_position != 0:
                self._shadow_flatten(price, reason="SWITCHING_TO_LIVE")
            self.total_shadow_pnl += self.shadow_pnl
            self._switch_to_live()
            return

        # Track unrealized shadow P&L
        if self.shadow_position != 0:
            unrealized = (price - self.shadow_entry_price) * self.shadow_position * POINT_VALUE * self.qty
            total = self.shadow_pnl + unrealized
            # Check threshold with unrealized
            if total <= -self.shadow_loss:
                self._shadow_flatten(price, reason="SWITCHING_TO_LIVE")
                self.total_shadow_pnl += self.shadow_pnl
                self._switch_to_live()
                return

        # Simulated trading logic
        if is_green and self.shadow_position != 1:
            if self.shadow_position == -1:
                self._shadow_flatten(price, reason="CANDLE_GREEN")
            if self.shadow_position == 0:
                self._shadow_enter(price, 1, "LONG")

        elif is_red and self.shadow_position != -1:
            if self.shadow_position == 1:
                self._shadow_flatten(price, reason="CANDLE_RED")
            if self.shadow_position == 0:
                self._shadow_enter(price, -1, "SHORT")

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
    # LIVE MODE - real trades
    # ==========================================================

    async def _live_tick(self, price: float, is_green: bool, is_red: bool):
        # Check if we hit live profit target
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

        # Trading logic
        if is_green and self.position != 1:
            if self.position == -1:
                await self._flatten(price, reason="CANDLE_GREEN")
            if self.position == 0:
                await self._enter_long(price)

        elif is_red and self.position != -1:
            if self.position == 1:
                await self._flatten(price, reason="CANDLE_RED")
            if self.position == 0:
                await self._enter_short(price)

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
            # Use direct market order to close - avoids double-order bug
            # with close_position_direct which can execute even on non-success
            close_side = 1 if self.position == 1 else 0  # sell to close long, buy to close short
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
    parser = argparse.ArgumentParser(description="TopstepX Candle Color Bot (Shadow Mode)")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--shadow-loss", type=float, default=700.0,
                        help="Shadow P&L loss threshold to switch to LIVE (default: 700)")
    parser.add_argument("--live-profit", type=float, default=500.0,
                        help="Live P&L profit target to switch back to SHADOW (default: 500)")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat/channel ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []

    bot = CandleColorBot(
        symbol=args.symbol,
        qty=args.qty,
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
