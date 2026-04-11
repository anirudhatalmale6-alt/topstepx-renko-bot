"""
TopstepX Renko Multi-TF Alignment Bot

Replicates the Pine Script session-enabled multi-TF Renko alignment strategy
with direct TopstepX API execution for minimal latency.

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    python bot.py --symbol NQ --brick-size 3 --qty 1
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

TIMEFRAMES = ["1min", "3min", "5min", "15min"]

# SDK only supports 1min, 5min, 15min, 30min (no 3min)
# We build 3min Renko from 1min data
SDK_TIMEFRAMES = ["1min", "5min", "15min"]


# ============================================================
# Session helpers
# ============================================================

def in_session() -> bool:
    """Check if current time is within trading session (ET)."""
    now = datetime.now(ET).time()
    return SESSION_START <= now <= SESSION_END


def session_just_ended(prev_in_session: bool) -> bool:
    """Detect session end transition."""
    return prev_in_session and not in_session()


# ============================================================
# Main Bot
# ============================================================

class RenkoAlignmentBot:
    def __init__(self, symbol: str, brick_size: float, qty: int = 1):
        self.symbol = symbol
        self.brick_size = brick_size
        self.qty = qty

        # Renko engines per timeframe
        self.engines: dict[str, RenkoEngine] = {
            tf: RenkoEngine(brick_size) for tf in TIMEFRAMES
        }

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

        # Session tracking
        self.was_in_session = False

        # SDK objects (set during run)
        self.suite = None
        self.running = False

    async def run(self):
        """Main bot loop."""
        from project_x_py import TradingSuite

        print(f"[BOT] Starting Renko Alignment Bot")
        print(f"[BOT] Symbol: {self.symbol}, Brick Size: {self.brick_size}, Qty: {self.qty}")
        print(f"[BOT] Session: {SESSION_START} - {SESSION_END} ET")
        print(f"[BOT] Timeframes: {TIMEFRAMES}")
        print()

        # Initialize TradingSuite - handles auth, websocket, data
        self.suite = await TradingSuite.create(
            self.symbol,
            timeframes=SDK_TIMEFRAMES,
            initial_days=2,  # Load 2 days of history to warm up Renko
        )

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.suite.instrument_info.id}")
        print()

        # Build Renko bricks from historical data
        await self._warm_up_renko()

        # Print initial state
        self._print_alignment()

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Bot running. Session active: {self.was_in_session}")
        print(f"[BOT] Press Ctrl+C to stop")
        print()

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(0.5)  # Check every 500ms
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _warm_up_renko(self):
        """Build Renko bricks from historical OHLC data for each timeframe."""
        print("[BOT] Warming up Renko engines from historical data...")

        # Direct SDK timeframes: 1min, 5min, 15min
        for tf_label in ["1min", "5min", "15min"]:
            data = await self.suite.data.get_data(tf_label, bars=500)
            if data is not None and len(data) > 0:
                bars = []
                for row in data.iter_rows(named=True):
                    bars.append({
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    })
                self.engines[tf_label].build_from_ohlc(bars)
                brick = self.engines[tf_label].last_brick
                direction = "BULL" if brick and brick.is_bullish else "BEAR"
                print(f"  {tf_label}: {len(self.engines[tf_label].bricks)} bricks, last={direction}")
            else:
                print(f"  {tf_label}: No data available!")

        # Build 3min from 1min data (aggregate every 3 bars)
        data_1m = await self.suite.data.get_data("1min", bars=1500)
        if data_1m is not None and len(data_1m) > 0:
            rows = list(data_1m.iter_rows(named=True))
            bars_3m = []
            for i in range(0, len(rows) - 2, 3):
                chunk = rows[i:i+3]
                bars_3m.append({
                    "open": float(chunk[0]["open"]),
                    "high": max(float(r["high"]) for r in chunk),
                    "low": min(float(r["low"]) for r in chunk),
                    "close": float(chunk[-1]["close"]),
                })
            self.engines["3min"].build_from_ohlc(bars_3m)
            brick = self.engines["3min"].last_brick
            direction = "BULL" if brick and brick.is_bullish else "BEAR"
            print(f"  3min: {len(self.engines['3min'].bricks)} bricks (from 1m agg), last={direction}")
        else:
            print(f"  3min: No 1min data available for aggregation!")

    async def _tick(self):
        """Single tick of the bot loop."""
        # Get current price
        price = await self.suite.data.get_current_price()
        if price is None:
            return

        # Update all Renko engines with current price
        any_new_bricks = False
        for tf_label in TIMEFRAMES:
            new_count = self.engines[tf_label].update(price)
            if new_count > 0:
                any_new_bricks = True

        # Check session state
        currently_in_session = in_session()
        sess_ended = self.was_in_session and not currently_in_session

        # Flatten on session end
        if sess_ended and self.position != 0:
            print(f"[SESSION] Session ended - flattening position")
            await self._flatten(price, reason="SESSION_END")
            self.was_in_session = currently_in_session
            return

        self.was_in_session = currently_in_session

        # Only trade during session
        if not currently_in_session:
            return

        # Check alignment
        all_bull = all(e.is_bullish is True for e in self.engines.values())
        all_bear = all(e.is_bearish is True for e in self.engines.values())

        # Exit logic
        if self.position == 1 and not all_bull:
            await self._flatten(price, reason="ALIGNMENT_BREAK")
        elif self.position == -1 and not all_bear:
            await self._flatten(price, reason="ALIGNMENT_BREAK")

        # Entry logic
        if self.position == 0:
            if all_bull:
                await self._enter_long(price)
            elif all_bear:
                await self._enter_short(price)

        # Print status on brick changes
        if any_new_bricks:
            self._print_alignment()

    async def _enter_long(self, price: float):
        """Place a market buy order."""
        print(f"\n[TRADE] >>> ENTERING LONG @ {price:.2f}")
        try:
            response = await self.suite.orders.place_market_order(
                contract_id=self.suite.instrument_info.id,
                side=0,  # Buy
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
        """Place a market sell order."""
        print(f"\n[TRADE] >>> ENTERING SHORT @ {price:.2f}")
        try:
            response = await self.suite.orders.place_market_order(
                contract_id=self.suite.instrument_info.id,
                side=1,  # Sell
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
        """Close current position."""
        direction = "LONG" if self.position == 1 else "SHORT"
        pnl = (price - self.entry_price) * self.position
        print(f"\n[TRADE] <<< EXITING {direction} @ {price:.2f} | P&L: {pnl:+.2f} pts | Reason: {reason}")

        try:
            result = await self.suite.positions.close_position_direct(
                contract_id=self.suite.instrument_info.id,
            )
            if result.get("success"):
                print(f"[TRADE] Position closed. Order: {result.get('orderId')}")
            else:
                # Fallback: place opposite market order
                side = 1 if self.position == 1 else 0
                response = await self.suite.orders.place_market_order(
                    contract_id=self.suite.instrument_info.id,
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

    def _print_alignment(self):
        """Print current Renko alignment status."""
        now = datetime.now(ET).strftime("%H:%M:%S")
        parts = []
        for tf in TIMEFRAMES:
            engine = self.engines[tf]
            if engine.is_bullish:
                parts.append(f"{tf}=BULL")
            elif engine.is_bearish:
                parts.append(f"{tf}=BEAR")
            else:
                parts.append(f"{tf}=----")

        all_bull = all(e.is_bullish is True for e in self.engines.values())
        all_bear = all(e.is_bearish is True for e in self.engines.values())
        align = "ALL BULL" if all_bull else ("ALL BEAR" if all_bear else "NO ALIGN")

        pos_str = "LONG" if self.position == 1 else ("SHORT" if self.position == -1 else "FLAT")

        print(f"[{now}] {' | '.join(parts)} | {align} | POS: {pos_str}")

    async def _shutdown(self):
        """Clean shutdown."""
        print("\n[BOT] Shutting down...")
        if self.position != 0:
            price = await self.suite.data.get_current_price()
            if price:
                await self._flatten(price, reason="SHUTDOWN")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Renko Alignment Bot")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol (NQ, ES, MNQ, MES)")
    parser.add_argument("--brick-size", type=float, required=True, help="Renko brick size in points")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity (contracts)")
    args = parser.parse_args()

    bot = RenkoAlignmentBot(
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
