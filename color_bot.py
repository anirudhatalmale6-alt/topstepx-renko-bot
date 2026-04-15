"""
TopstepX Multi-Timeframe Candle Close Strategy (Shadow Mode)

Strategy: All 4 timeframes (1min, 5min, 15min, 30min) must have their last
CLOSED candle be the same color to enter a position.
- All green = LONG
- All red = SHORT
- Any disagreement = EXIT and wait for next alignment

Exit: When ANY single timeframe's candle closes the opposite color.

Shadow Mode: Bot starts in SHADOW mode, simulating trades.
Once shadow P&L hits loss threshold (e.g. -$500), switches to LIVE.
LIVE places real trades until profit target (e.g. +$500), then back to SHADOW.

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python color_bot.py --symbol NQ --qty 1 --shadow-loss 500 --live-profit 500
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

TIMEFRAMES = ["1min", "5min", "15min", "30min"]

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

class MultiTFBot:
    def __init__(self, symbol: str, qty: int = 1,
                 shadow_loss: float = 500.0, live_profit: float = 500.0,
                 tg_token: str = "", tg_chat: str = "", tg_keys: list = None):
        self.symbol = symbol
        self.qty = qty
        self.shadow_loss = shadow_loss
        self.live_profit = live_profit
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_keys = tg_keys or []

        # Mode: "SHADOW" or "LIVE"
        self.mode = "SHADOW"

        # Real position state (LIVE mode only)
        self.position = 0       # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

        # Shadow position state
        self.shadow_position = 0
        self.shadow_entry_price = 0.0
        self.shadow_pnl = 0.0

        # Live P&L tracking (resets each cycle)
        self.live_pnl = 0.0

        # Total session stats
        self.total_live_pnl = 0.0
        self.total_shadow_pnl = 0.0
        self.live_cycles = 0

        # Multi-timeframe candle color tracking
        # Stores the color of the LAST CLOSED candle for each timeframe
        # None = no candle yet, "GREEN" or "RED"
        self.tf_colors = {tf: None for tf in TIMEFRAMES}

        # Track whether we have received at least one closed candle per TF
        self.tf_initialized = {tf: False for tf in TIMEFRAMES}

        # Session tracking
        self.was_in_session = False

        # SDK objects
        self.suite = None
        self.ctx = None
        self.running = False

        # Lock for thread-safe event handling
        self._lock = asyncio.Lock()

    async def run(self):
        from project_x_py import TradingSuite, EventType

        print(f"[BOT] Multi-Timeframe Candle Close Strategy - SHADOW MODE")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Timeframes: {', '.join(TIMEFRAMES)}")
        print(f"[BOT] Entry: ALL timeframes same color (closed candles)")
        print(f"[BOT] Exit: ANY timeframe closes opposite color")
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
            timeframes=TIMEFRAMES,
            initial_days=1,
        )

        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        # Load last closed candle color for each timeframe from historical data
        await self._init_candle_colors()

        # Register for NEW_BAR events - fires when any timeframe candle closes
        await self.ctx.event_bus.on(EventType.NEW_BAR, self._on_new_bar)

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Starting in SHADOW mode")
        print(f"[BOT] Press Ctrl+C to stop")
        print()

        try:
            while self.running:
                await self._session_check()
                # Check profit target between candle closes
                async with self._lock:
                    await self._check_thresholds()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _init_candle_colors(self):
        """Load last closed candle color for each timeframe from history."""
        for tf in TIMEFRAMES:
            try:
                data = await self.ctx.data.get_data(tf, bars=2)
                if data is not None and len(data) >= 2:
                    # The last bar is the CURRENT (still open) bar
                    # The second-to-last bar is the last CLOSED bar
                    rows = list(data.iter_rows(named=True))
                    closed_bar = rows[-2]
                    o = float(closed_bar["open"])
                    c = float(closed_bar["close"])
                    if c > o:
                        self.tf_colors[tf] = "GREEN"
                    elif c < o:
                        self.tf_colors[tf] = "RED"
                    else:
                        self.tf_colors[tf] = None  # doji - neutral
                    self.tf_initialized[tf] = True
                    print(f"[INIT] {tf} last closed candle: {self.tf_colors[tf]} (O:{o:.2f} C:{c:.2f})")
                elif data is not None and len(data) == 1:
                    # Only one bar available - it's the current open bar, no closed yet
                    print(f"[INIT] {tf} - only 1 bar available, waiting for first close")
                else:
                    print(f"[INIT] {tf} - no data available yet")
            except Exception as e:
                print(f"[INIT] {tf} error: {e}")

        # Show alignment status
        self._print_alignment()

    async def _on_new_bar(self, event):
        """Called when a candle closes on any timeframe."""
        async with self._lock:
            tf = event.data["timeframe"]
            bar = event.data["data"]

            if tf not in self.tf_colors:
                return  # Not one of our tracked timeframes

            o = float(bar["open"])
            c = float(bar["close"])

            old_color = self.tf_colors[tf]

            if c > o:
                new_color = "GREEN"
            elif c < o:
                new_color = "RED"
            else:
                new_color = old_color  # doji - keep previous color

            self.tf_colors[tf] = new_color
            self.tf_initialized[tf] = True

            now = datetime.now(ET).strftime("%H:%M:%S")

            changed = old_color != new_color
            marker = " <<<" if changed else ""
            print(f"[{now}] [CANDLE] {tf} closed: {new_color} (O:{o:.2f} C:{c:.2f}){marker}")

            if not in_session():
                return

            # Get current price for P&L and trade execution
            price = await self.ctx.data.get_current_price()
            if price is None:
                return

            # Check alignment and act
            await self._evaluate(price)

    async def _evaluate(self, price: float):
        """Check timeframe alignment and enter/exit accordingly."""
        now = datetime.now(ET).strftime("%H:%M:%S")

        # Make sure all timeframes have data
        if not all(self.tf_initialized.values()):
            missing = [tf for tf, init in self.tf_initialized.items() if not init]
            print(f"[{now}] Waiting for candle data: {', '.join(missing)}")
            return

        colors = self.tf_colors
        all_green = all(c == "GREEN" for c in colors.values())
        all_red = all(c == "RED" for c in colors.values())
        aligned = all_green or all_red

        color_str = " | ".join(f"{tf}={colors[tf]}" for tf in TIMEFRAMES)
        print(f"[{now}] [{self.mode}] Alignment: {color_str}")

        if self.mode == "SHADOW":
            await self._shadow_evaluate(price, all_green, all_red, aligned, now)
        else:
            await self._live_evaluate(price, all_green, all_red, aligned, now)

    # ==========================================================
    # SHADOW MODE
    # ==========================================================

    async def _shadow_evaluate(self, price: float, all_green: bool, all_red: bool, aligned: bool, now: str):
        """Shadow mode: simulate trades, check loss threshold."""

        # Check unrealized P&L for threshold
        if self.shadow_position != 0:
            unrealized = (price - self.shadow_entry_price) * self.shadow_position * POINT_VALUE * self.qty
            total = self.shadow_pnl + unrealized
            if total <= -self.shadow_loss:
                self._shadow_flatten(price, reason="LOSS_THRESHOLD")
                self.total_shadow_pnl += self.shadow_pnl
                self._switch_to_live()
                return

        # Check realized P&L threshold
        if self.shadow_pnl <= -self.shadow_loss:
            if self.shadow_position != 0:
                self._shadow_flatten(price, reason="LOSS_THRESHOLD")
            self.total_shadow_pnl += self.shadow_pnl
            self._switch_to_live()
            return

        # Trading logic
        if aligned:
            target_pos = 1 if all_green else -1
            target_label = "LONG" if all_green else "SHORT"

            if self.shadow_position == 0:
                # Enter new position
                self._shadow_enter(price, target_pos, target_label)
            elif self.shadow_position != target_pos:
                # Flip: exit current, enter new
                self._shadow_flatten(price, reason=f"FLIP_TO_{target_label}")
                self._shadow_enter(price, target_pos, target_label)
            # else: already in correct direction, hold
        else:
            # Not aligned - exit if in position
            if self.shadow_position != 0:
                self._shadow_flatten(price, reason="MISALIGNMENT")

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

    async def _live_evaluate(self, price: float, all_green: bool, all_red: bool, aligned: bool, now: str):
        """Live mode: place real orders, check profit target."""

        # Check profit target with unrealized
        if self.position != 0:
            unrealized = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
            total = self.live_pnl + unrealized
            if total >= self.live_profit:
                print(f"\n[{now}] [LIVE] *** PROFIT TARGET HIT! Live P&L: ${total:.2f} ***")
                await self._flatten(price, reason="PROFIT_TARGET")
                self.total_live_pnl += self.live_pnl
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0)
                self._switch_to_shadow()
                return

        # Trading logic
        if aligned:
            target_pos = 1 if all_green else -1
            target_label = "LONG" if all_green else "SHORT"

            if self.position == 0:
                # Enter new position
                if target_pos == 1:
                    await self._enter_long(price)
                else:
                    await self._enter_short(price)
            elif self.position != target_pos:
                # Flip: exit current, enter new
                await self._flatten(price, reason=f"FLIP_TO_{target_label}")
                if target_pos == 1:
                    await self._enter_long(price)
                else:
                    await self._enter_short(price)
            # else: already in correct direction, hold
        else:
            # Not aligned - exit if in position
            if self.position != 0:
                await self._flatten(price, reason="MISALIGNMENT")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0)

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
        self._print_alignment()
        print(f"[{now}] [MODE] ========================================\n")

    def _switch_to_shadow(self):
        self.mode = "SHADOW"
        self.shadow_pnl = 0.0
        self.shadow_position = 0
        self.shadow_entry_price = 0.0
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [MODE] ========================================")
        print(f"[{now}] [MODE] >>> SWITCHING TO SHADOW")
        print(f"[{now}] [MODE] Live P&L was: ${self.live_pnl:.2f} (total live: ${self.total_live_pnl:.2f})")
        print(f"[{now}] [MODE] Waiting for -${self.shadow_loss:.0f} before going live again...")
        print(f"[{now}] [MODE] ========================================\n")

    # ==========================================================
    # Session management
    # ==========================================================

    async def _session_check(self):
        """Periodic check for session start/end."""
        currently_in_session = in_session()

        # Session ended
        if self.was_in_session and not currently_in_session:
            now = datetime.now(ET).strftime("%H:%M:%S")
            print(f"\n[{now}] [SESSION] Session ended")
            price = await self.ctx.data.get_current_price()
            if price:
                if self.position != 0:
                    await self._flatten(price, reason="SESSION_END")
                    send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                                 "FLAT", self.symbol, price, 0)
                if self.shadow_position != 0:
                    self._shadow_flatten(price, reason="SESSION_END")

        # Session started
        if not self.was_in_session and currently_in_session:
            now = datetime.now(ET).strftime("%H:%M:%S")
            self.mode = "SHADOW"
            self.shadow_pnl = 0.0
            self.shadow_position = 0
            self.shadow_entry_price = 0.0
            self.live_pnl = 0.0
            self.total_live_pnl = 0.0
            self.total_shadow_pnl = 0.0
            self.live_cycles = 0
            # Re-init candle colors from current data
            await self._init_candle_colors()
            print(f"[{now}] [SESSION] New session started - SHADOW mode")

        self.was_in_session = currently_in_session

    # ==========================================================
    # Profit target check on ticks (runs every second)
    # ==========================================================

    async def _check_thresholds(self):
        """Check profit target and shadow loss between candle closes."""
        if not in_session():
            return

        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        if self.mode == "LIVE" and self.position != 0:
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

        elif self.mode == "SHADOW" and self.shadow_position != 0:
            unrealized = (price - self.shadow_entry_price) * self.shadow_position * POINT_VALUE * self.qty
            total = self.shadow_pnl + unrealized
            if total <= -self.shadow_loss:
                self._shadow_flatten(price, reason="LOSS_THRESHOLD")
                self.total_shadow_pnl += self.shadow_pnl
                self._switch_to_live()

    # ==========================================================
    # Helpers
    # ==========================================================

    def _print_alignment(self):
        color_str = " | ".join(f"{tf}={self.tf_colors[tf] or '?'}" for tf in TIMEFRAMES)
        colors = [c for c in self.tf_colors.values() if c is not None]
        if colors:
            all_same = len(set(colors)) == 1
            status = f"ALIGNED ({colors[0]})" if all_same and len(colors) == len(TIMEFRAMES) else "NOT ALIGNED"
        else:
            status = "WAITING"
        print(f"[ALIGN] {color_str} => {status}")

    async def _shutdown(self):
        print("\n[BOT] Shutting down...")
        price = await self.ctx.data.get_current_price()
        if price:
            if self.position != 0:
                await self._flatten(price, reason="SHUTDOWN")
                send_signals(self.tg_token, self.tg_chat, self.tg_keys,
                             "FLAT", self.symbol, price, 0)
            if self.shadow_position != 0:
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
    parser = argparse.ArgumentParser(description="TopstepX Multi-TF Candle Close Bot (Shadow Mode)")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--shadow-loss", type=float, default=500.0,
                        help="Shadow P&L loss threshold to switch to LIVE (default: 500)")
    parser.add_argument("--live-profit", type=float, default=500.0,
                        help="Live P&L profit target to switch back to SHADOW (default: 500)")
    parser.add_argument("--tg-token", default="", help="Telegram bot token")
    parser.add_argument("--tg-chat", default="", help="Telegram chat/channel ID")
    parser.add_argument("--tg-keys", default="", help="Comma-separated passkeys")
    args = parser.parse_args()

    keys = [k.strip() for k in args.tg_keys.split(",") if k.strip()] if args.tg_keys else []

    bot = MultiTFBot(
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
