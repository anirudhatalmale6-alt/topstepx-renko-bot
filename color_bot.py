"""
TopstepX Realtime Candle Color Strategy Bot

Strategy: Trades based on the current 15-minute candle color.
- Green candle (close > open) = LONG
- Red candle (close < open) = SHORT
- Doji (body < 1% of range) = ignored

Session: 6:00 PM - 4:00 PM ET (full NQ futures session)

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python color_bot.py --symbol NQ --qty 1
"""

import asyncio
import argparse
import signal
from datetime import datetime, time as dtime

import pytz


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

# Full NQ session: 6:00 PM ET to 4:00 PM ET next day
# Since this crosses midnight, we check if we're NOT in the break (4:00 PM - 6:00 PM)
SESSION_BREAK_START = dtime(16, 0)
SESSION_BREAK_END = dtime(18, 0)

TIMEFRAME = "15min"
DOJI_THRESHOLD = 0.01  # 1% of candle range


# ============================================================
# Session helpers
# ============================================================

def in_session() -> bool:
    """Check if current time is within trading session.
    Session runs 18:00 - 16:00 ET (22 hours, break 16:00-18:00)."""
    now = datetime.now(ET).time()
    # We're OUT of session during the break (4 PM - 6 PM ET)
    if SESSION_BREAK_START <= now < SESSION_BREAK_END:
        return False
    return True


# ============================================================
# Main Bot
# ============================================================

class CandleColorBot:
    def __init__(self, symbol: str, qty: int = 1):
        self.symbol = symbol
        self.qty = qty

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

        # Candle tracking
        self.current_candle_open = None
        self.last_candle_time = None

        # Session tracking
        self.was_in_session = False

        # SDK objects
        self.suite = None
        self.ctx = None
        self.running = False

    async def run(self):
        """Main bot loop."""
        from project_x_py import TradingSuite

        print(f"[BOT] Realtime Candle Color Bot")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Timeframe: {TIMEFRAME}")
        print(f"[BOT] Session: 18:00 - 16:00 ET (break 16:00-18:00)")
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

        # Get current candle open from latest data
        await self._init_candle()

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Session active: {self.was_in_session}")
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
        """Get the current 15min candle's open price."""
        data = await self.ctx.data.get_data(TIMEFRAME, bars=1)
        if data is not None and len(data) > 0:
            rows = list(data.iter_rows(named=True))
            last_row = rows[-1]
            self.current_candle_open = float(last_row["open"])
            self.last_candle_time = last_row.get("timestamp") or last_row.get("time")
            print(f"[BOT] Current {TIMEFRAME} candle open: {self.current_candle_open:.2f}")
        else:
            print("[BOT] No candle data available!")

    async def _tick(self):
        """Single tick of the bot loop."""
        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        # Check if a new candle has started by re-fetching the latest bar
        data = await self.ctx.data.get_data(TIMEFRAME, bars=1)
        if data is not None and len(data) > 0:
            rows = list(data.iter_rows(named=True))
            last_row = rows[-1]
            candle_time = last_row.get("timestamp") or last_row.get("time")
            candle_open = float(last_row["open"])

            # Detect new candle
            if candle_time != self.last_candle_time:
                self.current_candle_open = candle_open
                self.last_candle_time = candle_time
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] New {TIMEFRAME} candle opened @ {candle_open:.2f}")

        if self.current_candle_open is None:
            return

        # Calculate candle color in real-time
        candle_high = price  # simplified - using current price
        candle_low = price
        candle_range = candle_high - candle_low if candle_high != candle_low else 1.0
        body = abs(price - self.current_candle_open)

        is_doji = body <= candle_range * DOJI_THRESHOLD
        is_green = price > self.current_candle_open and not is_doji
        is_red = price < self.current_candle_open and not is_doji

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

        # Trading logic
        if is_green and self.position != 1:
            # Need to go long
            if self.position == -1:
                await self._flatten(price, reason="CANDLE_GREEN")
            if self.position == 0:
                await self._enter_long(price)

        elif is_red and self.position != -1:
            # Need to go short
            if self.position == 1:
                await self._flatten(price, reason="CANDLE_RED")
            if self.position == 0:
                await self._enter_short(price)

    async def _enter_long(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [TRADE] >>> ENTERING LONG @ {price:.2f} (candle GREEN, open={self.current_candle_open:.2f})")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=0,
                size=self.qty,
            )
            if response.success:
                self.position = 1
                self.entry_price = price
                print(f"[TRADE] Order filled. ID: {response.orderId}")
            else:
                print(f"[TRADE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Order ERROR: {e}")

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [TRADE] >>> ENTERING SHORT @ {price:.2f} (candle RED, open={self.current_candle_open:.2f})")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=1,
                size=self.qty,
            )
            if response.success:
                self.position = -1
                self.entry_price = price
                print(f"[TRADE] Order filled. ID: {response.orderId}")
            else:
                print(f"[TRADE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Order ERROR: {e}")

    async def _flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl = (price - self.entry_price) * self.position
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [TRADE] <<< EXITING {direction} @ {price:.2f} | P&L: {pnl:+.2f} pts | Reason: {reason}")

        try:
            result = await self.ctx.positions.close_position_direct(
                contract_id=self.ctx.instrument_info.id,
            )
            if result.get("success"):
                print(f"[TRADE] Position closed. Order: {result.get('orderId')}")
            else:
                side = 1 if self.position == 1 else 0
                response = await self.ctx.orders.place_market_order(
                    contract_id=self.ctx.instrument_info.id,
                    side=side,
                    size=self.qty,
                )
                if response.success:
                    print(f"[TRADE] Closed via market order. ID: {response.orderId}")
                else:
                    print(f"[TRADE] CLOSE FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Close ERROR: {e}")

        self.position = 0
        self.entry_price = 0.0

    async def _shutdown(self):
        print("\n[BOT] Shutting down...")
        if self.position != 0:
            price = await self.ctx.data.get_current_price()
            if price:
                await self._flatten(price, reason="SHUTDOWN")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Candle Color Bot")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    args = parser.parse_args()

    bot = CandleColorBot(
        symbol=args.symbol,
        qty=args.qty,
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
