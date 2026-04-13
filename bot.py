"""
TopstepX Renko Ghost Candle + 9 EMA Bot

Strategy: Ghost candle (forming brick) crossing the 9 EMA on Renko chart.
- Ghost candle crosses ABOVE 9 EMA = LONG
- Ghost candle crosses BELOW 9 EMA = SHORT

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python bot.py --symbol NQ --brick-size 0.25 --qty 1
"""

import asyncio
import argparse
import signal
import sys
from datetime import datetime, time as dtime

import pytz

from renko import RenkoEngine


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(9, 27)
SESSION_END = dtime(16, 0)

EMA_PERIOD = 9
EMA_BUFFER = 0.50  # Must be this many points past EMA to count as crossed
TRADE_COOLDOWN = 30  # Seconds between trades to prevent rapid flipping


# ============================================================
# Session helpers
# ============================================================

def in_session() -> bool:
    now = datetime.now(ET).time()
    return SESSION_START <= now <= SESSION_END


# ============================================================
# Main Bot
# ============================================================

class GhostEmaBot:
    def __init__(self, symbol: str, brick_size: float, qty: int = 1):
        self.symbol = symbol
        self.brick_size = brick_size
        self.qty = qty

        # Single Renko engine (no multi-timeframe)
        self.engine = RenkoEngine(brick_size)

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

        # Ghost vs EMA state for crossover detection
        self.prev_side = None  # "ABOVE" or "BELOW"
        self.last_trade_time = 0  # timestamp of last trade

        # Session tracking
        self.was_in_session = False

        # SDK objects
        self.suite = None
        self.ctx = None
        self.running = False

    async def run(self):
        """Main bot loop."""
        from project_x_py import TradingSuite

        print(f"[BOT] Ghost Candle + 9 EMA Bot (5min Renko)")
        print(f"[BOT] Symbol: {self.symbol}, Brick Size: {self.brick_size}, Qty: {self.qty}")
        print(f"[BOT] Session: {SESSION_START} - {SESSION_END} ET")
        print(f"[BOT] EMA Period: {EMA_PERIOD}")
        print()

        import os
        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["5min"],
            initial_days=2,
        )

        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        # Build Renko from historical 1min data
        await self._warm_up()

        self.running = True
        self.was_in_session = in_session()

        ema_val = self.engine.ema(EMA_PERIOD)
        side = self.engine.ghost_vs_ema(EMA_PERIOD, buffer=EMA_BUFFER)
        self.prev_side = side

        print(f"[BOT] Renko bricks: {len(self.engine.bricks)}")
        print(f"[BOT] Current EMA({EMA_PERIOD}): {ema_val:.2f}" if ema_val else "[BOT] EMA: not enough data")
        print(f"[BOT] Ghost vs EMA: {side}")
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

    async def _warm_up(self):
        """Build Renko bricks from historical data."""
        print("[BOT] Warming up Renko from historical data...")

        data = await self.ctx.data.get_data("5min", bars=500)
        if data is not None and len(data) > 0:
            bars = []
            for row in data.iter_rows(named=True):
                bars.append({
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                })
            self.engine.build_from_ohlc(bars)
            brick = self.engine.last_brick
            direction = "BULL" if brick and brick.is_bullish else "BEAR"
            print(f"[BOT] {len(self.engine.bricks)} bricks built, last={direction}")
        else:
            print("[BOT] No historical data available!")

    async def _tick(self):
        """Single tick of the bot loop."""
        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        # Update Renko engine (creates new bricks if price moved enough)
        new_bricks = self.engine.update(price)

        # Get ghost candle position relative to EMA
        ema_val = self.engine.ema(EMA_PERIOD)
        if ema_val is None:
            return

        side = self.engine.ghost_vs_ema(EMA_PERIOD, buffer=EMA_BUFFER)
        if side is None or side == "NEUTRAL":
            return

        # Check session
        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        # Flatten on session end
        if sess_ended and self.position != 0:
            print(f"[SESSION] Session ended - flattening")
            await self._flatten(price, reason="SESSION_END")
            self.was_in_session = currently_in_session
            self.prev_side = side
            return

        self.was_in_session = currently_in_session

        if not currently_in_session:
            self.prev_side = side
            return

        # Detect crossover
        crossed_above = self.prev_side == "BELOW" and side == "ABOVE"
        crossed_below = self.prev_side == "ABOVE" and side == "BELOW"

        # Print on crossover or new bricks
        if crossed_above or crossed_below or new_bricks > 0:
            now = datetime.now(ET).strftime("%H:%M:%S")
            pos_str = "LONG" if self.position == 1 else ("SHORT" if self.position == -1 else "FLAT")
            cross_str = ""
            if crossed_above:
                cross_str = " ** CROSS ABOVE **"
            elif crossed_below:
                cross_str = " ** CROSS BELOW **"
            print(f"[{now}] Price={price:.2f} EMA={ema_val:.2f} Ghost={side} POS={pos_str}{cross_str}")

        # Cooldown check
        import time as _time
        now_ts = _time.time()
        on_cooldown = (now_ts - self.last_trade_time) < TRADE_COOLDOWN

        # Trading logic on crossover
        if crossed_above and not on_cooldown:
            if self.position == -1:
                await self._flatten(price, reason="GHOST_CROSS_ABOVE")
                self.last_trade_time = _time.time()
            if self.position == 0:
                await self._enter_long(price)
                self.last_trade_time = _time.time()

        elif crossed_below and not on_cooldown:
            if self.position == 1:
                await self._flatten(price, reason="GHOST_CROSS_BELOW")
                self.last_trade_time = _time.time()
            if self.position == 0:
                await self._enter_short(price)
                self.last_trade_time = _time.time()

        self.prev_side = side

    async def _enter_long(self, price: float):
        print(f"\n[TRADE] >>> ENTERING LONG @ {price:.2f}")
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
        print(f"\n[TRADE] >>> ENTERING SHORT @ {price:.2f}")
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
        print(f"\n[TRADE] <<< EXITING {direction} @ {price:.2f} | P&L: {pnl:+.2f} pts | Reason: {reason}")

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
    parser = argparse.ArgumentParser(description="TopstepX Ghost Candle + EMA Bot")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--brick-size", type=float, required=True, help="Renko brick size")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    args = parser.parse_args()

    bot = GhostEmaBot(
        symbol=args.symbol,
        brick_size=args.brick_size,
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
