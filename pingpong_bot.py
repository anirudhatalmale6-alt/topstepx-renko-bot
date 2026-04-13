"""
TopstepX Ping Pong Breakout Bot

Strategy: Uses previous 15min candle body (top/bottom) as flip zones.
- At TOP level: price above = LONG, price below = SHORT
- At BOTTOM level: price above = LONG, price below = SHORT
- Trailing stop locks in profits
- Levels update every 15 minutes

Session: 6:00 PM - 4:00 PM ET

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python pingpong_bot.py --symbol NQ --qty 1
"""

import asyncio
import argparse
import signal
import time
from datetime import datetime, time as dtime

import pytz


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_BREAK_START = dtime(16, 0)
SESSION_BREAK_END = dtime(18, 0)

TIMEFRAME = "15min"
POINT_VALUE = 20.0  # NQ: $20 per point per contract

# Trailing stop
TRAIL_POINTS = 2.0  # Trail 2 points behind best price


# ============================================================
# Session helpers
# ============================================================

def in_session() -> bool:
    now = datetime.now(ET).time()
    if SESSION_BREAK_START <= now < SESSION_BREAK_END:
        return False
    return True


# ============================================================
# Main Bot
# ============================================================

class PingPongBot:
    def __init__(self, symbol: str, qty: int = 1, trail: float = TRAIL_POINTS):
        self.symbol = symbol
        self.qty = qty
        self.trail = trail

        # Levels from previous candle body
        self.body_top = None    # Higher of open/close
        self.body_bottom = None  # Lower of open/close
        self.last_candle_time = None

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat
        self.entry_price = 0.0
        self.best_price = 0.0
        self.active_level = None  # Which level triggered the entry: "TOP" or "BOTTOM"

        # Tracking which side of each level we're on
        self.prev_side_top = None     # "ABOVE" or "BELOW" the top level
        self.prev_side_bottom = None  # "ABOVE" or "BELOW" the bottom level

        # P&L tracking
        self.session_pnl = 0.0
        self.trade_count = 0

        # Session
        self.was_in_session = False

        # SDK
        self.suite = None
        self.ctx = None
        self.running = False

    async def run(self):
        from project_x_py import TradingSuite

        print(f"[BOT] Ping Pong Breakout Bot")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Timeframe: {TIMEFRAME}")
        print(f"[BOT] Trailing Stop: {self.trail} pts")
        print(f"[BOT] Session: 18:00 - 16:00 ET")
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

        # Get previous candle levels
        await self._init_levels()

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Press Ctrl+C to stop")
        print()

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _init_levels(self):
        """Get the previous 15min candle's body top and bottom."""
        data = await self.ctx.data.get_data(TIMEFRAME, bars=2)
        if data is not None and len(data) >= 2:
            rows = list(data.iter_rows(named=True))
            prev = rows[-2]  # Previous closed candle
            current = rows[-1]  # Current forming candle

            prev_open = float(prev["open"])
            prev_close = float(prev["close"])
            self.body_top = max(prev_open, prev_close)
            self.body_bottom = min(prev_open, prev_close)
            self.last_candle_time = current.get("timestamp") or current.get("time")

            print(f"[BOT] Previous candle body: TOP={self.body_top:.2f} BOTTOM={self.body_bottom:.2f}")
            print(f"[BOT] Range: {self.body_top - self.body_bottom:.2f} pts")
        else:
            print("[BOT] Not enough candle data!")

    async def _update_levels(self):
        """Check if new candle started and update levels."""
        data = await self.ctx.data.get_data(TIMEFRAME, bars=2)
        if data is not None and len(data) >= 2:
            rows = list(data.iter_rows(named=True))
            current = rows[-1]
            candle_time = current.get("timestamp") or current.get("time")

            if candle_time != self.last_candle_time:
                # New candle started - previous candle is now rows[-2]
                prev = rows[-2]
                prev_open = float(prev["open"])
                prev_close = float(prev["close"])

                old_top = self.body_top
                old_bottom = self.body_bottom
                self.body_top = max(prev_open, prev_close)
                self.body_bottom = min(prev_open, prev_close)
                self.last_candle_time = candle_time

                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"\n[{now}] NEW LEVELS: TOP={self.body_top:.2f} BOTTOM={self.body_bottom:.2f} (range={self.body_top - self.body_bottom:.2f})")

                # Reset side tracking for new levels
                self.prev_side_top = None
                self.prev_side_bottom = None

    async def _tick(self):
        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        # Update levels on new candle
        await self._update_levels()

        if self.body_top is None or self.body_bottom is None:
            return

        # Check session
        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        if sess_ended and self.position != 0:
            print(f"[SESSION] Session break - flattening")
            await self._flatten(price, reason="SESSION_BREAK")
            self.was_in_session = currently_in_session
            return

        self.was_in_session = currently_in_session

        if not currently_in_session:
            return

        # Determine which side of each level price is on
        side_top = "ABOVE" if price > self.body_top else "BELOW"
        side_bottom = "ABOVE" if price > self.body_bottom else "BELOW"

        # === MANAGE EXISTING POSITION ===
        if self.position != 0:
            # Update best price for trailing stop
            if self.position == 1:
                self.best_price = max(self.best_price, price)
                trail_stop = self.best_price - self.trail
                if price <= trail_stop:
                    await self._flatten(price, reason=f"TRAIL_STOP (best={self.best_price:.2f})")
                    self._update_sides(side_top, side_bottom)
                    return
            else:
                self.best_price = min(self.best_price, price)
                trail_stop = self.best_price + self.trail
                if price >= trail_stop:
                    await self._flatten(price, reason=f"TRAIL_STOP (best={self.best_price:.2f})")
                    self._update_sides(side_top, side_bottom)
                    return

            # Check for flip at the active level
            if self.active_level == "TOP":
                # Was long (above top), now below top = flip short
                if self.position == 1 and side_top == "BELOW" and self.prev_side_top == "ABOVE":
                    await self._flatten(price, reason="FLIP_AT_TOP")
                    await self._enter_short(price, "TOP")
                # Was short (below top), now above top = flip long
                elif self.position == -1 and side_top == "ABOVE" and self.prev_side_top == "BELOW":
                    await self._flatten(price, reason="FLIP_AT_TOP")
                    await self._enter_long(price, "TOP")

            elif self.active_level == "BOTTOM":
                # Was long (above bottom), now below bottom = flip short
                if self.position == 1 and side_bottom == "BELOW" and self.prev_side_bottom == "ABOVE":
                    await self._flatten(price, reason="FLIP_AT_BOTTOM")
                    await self._enter_short(price, "BOTTOM")
                # Was short (below bottom), now above bottom = flip long
                elif self.position == -1 and side_bottom == "ABOVE" and self.prev_side_bottom == "BELOW":
                    await self._flatten(price, reason="FLIP_AT_BOTTOM")
                    await self._enter_long(price, "BOTTOM")

        # === FLAT: look for breakout entry ===
        else:
            # Breakout at TOP level
            if self.prev_side_top is not None:
                if side_top == "ABOVE" and self.prev_side_top == "BELOW":
                    await self._enter_long(price, "TOP")
                elif side_top == "BELOW" and self.prev_side_top == "ABOVE":
                    await self._enter_short(price, "TOP")

            # Breakout at BOTTOM level
            if self.position == 0 and self.prev_side_bottom is not None:
                if side_bottom == "BELOW" and self.prev_side_bottom == "ABOVE":
                    await self._enter_short(price, "BOTTOM")
                elif side_bottom == "ABOVE" and self.prev_side_bottom == "BELOW":
                    await self._enter_long(price, "BOTTOM")

        self._update_sides(side_top, side_bottom)

    def _update_sides(self, side_top, side_bottom):
        self.prev_side_top = side_top
        self.prev_side_bottom = side_bottom

    async def _enter_long(self, price: float, level: str):
        now = datetime.now(ET).strftime("%H:%M:%S")
        self.trade_count += 1
        target = self.body_bottom if level == "TOP" else self.body_top
        print(f"\n[{now}] [#{self.trade_count}] >>> LONG @ {price:.2f} | Level: {level}={self.body_top if level == 'TOP' else self.body_bottom:.2f} | Session P&L: ${self.session_pnl:.2f}")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=0,
                size=self.qty,
            )
            if response.success:
                self.position = 1
                self.entry_price = price
                self.best_price = price
                self.active_level = level
                print(f"[TRADE] Filled. Trail stop: {price - self.trail:.2f}")
            else:
                print(f"[TRADE] FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] ERROR: {e}")

    async def _enter_short(self, price: float, level: str):
        now = datetime.now(ET).strftime("%H:%M:%S")
        self.trade_count += 1
        print(f"\n[{now}] [#{self.trade_count}] >>> SHORT @ {price:.2f} | Level: {level}={self.body_top if level == 'TOP' else self.body_bottom:.2f} | Session P&L: ${self.session_pnl:.2f}")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=1,
                size=self.qty,
            )
            if response.success:
                self.position = -1
                self.entry_price = price
                self.best_price = price
                self.active_level = level
                print(f"[TRADE] Filled. Trail stop: {price + self.trail:.2f}")
            else:
                print(f"[TRADE] FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] ERROR: {e}")

    async def _flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        trade_pnl_pts = (price - self.entry_price) * self.position
        trade_pnl_dollars = trade_pnl_pts * POINT_VALUE * self.qty
        self.session_pnl += trade_pnl_dollars

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [TRADE] <<< EXIT {direction} @ {price:.2f} | Trade: ${trade_pnl_dollars:+.2f} ({trade_pnl_pts:+.2f} pts) | Session: ${self.session_pnl:.2f} | {reason}")

        try:
            result = await self.ctx.positions.close_position_direct(
                contract_id=self.ctx.instrument_info.id,
            )
            if result.get("success"):
                print(f"[TRADE] Closed. Order: {result.get('orderId')}")
            else:
                side = 1 if self.position == 1 else 0
                response = await self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=side,
                    size=self.qty,
                )
                if response.success:
                    print(f"[TRADE] Closed via market. ID: {response.orderId}")
                else:
                    print(f"[TRADE] CLOSE FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Close ERROR: {e}")

        self.position = 0
        self.entry_price = 0.0
        self.best_price = 0.0
        self.active_level = None

    async def _shutdown(self):
        print("\n[BOT] Shutting down...")
        if self.position != 0:
            price = await self.ctx.data.get_current_price()
            if price:
                await self._flatten(price, reason="SHUTDOWN")

        print(f"\n[BOT] === SESSION SUMMARY ===")
        print(f"[BOT] Total P&L: ${self.session_pnl:.2f}")
        print(f"[BOT] Trades: {self.trade_count}")
        print(f"[BOT] ========================")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Ping Pong Bot")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--trail", type=float, default=2.0, help="Trailing stop in points")
    args = parser.parse_args()

    bot = PingPongBot(
        symbol=args.symbol,
        qty=args.qty,
        trail=args.trail,
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
