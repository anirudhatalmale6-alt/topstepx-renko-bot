"""
TopstepX Momentum Scalper Bot

Strategy: Detects real-time price momentum and rides it with a trailing stop.
- Tracks price movement over a rolling window
- Enters when momentum exceeds threshold (price moving fast in one direction)
- Trails a stop behind the move to lock profits
- Flips direction when momentum reverses
- $100 TP milestones: banks profit and pauses, then continues

Session: 6:00 PM - 4:00 PM ET (full NQ futures session)

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python momentum_bot.py --symbol NQ --qty 1 --tp 100
"""

import asyncio
import argparse
import signal
import time
from collections import deque
from datetime import datetime, time as dtime

import pytz


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_BREAK_START = dtime(16, 0)
SESSION_BREAK_END = dtime(18, 0)

# NQ: 1 point = $20 per contract
POINT_VALUE = 20.0

# Momentum detection
MOMENTUM_WINDOW = 10.0      # Seconds to measure momentum over
MOMENTUM_THRESHOLD = 2.0    # Points of movement needed to trigger entry
MOMENTUM_COOLDOWN = 5.0     # Seconds after exit before re-entering

# Risk management
TRAILING_STOP = 3.0         # Points behind best price
MAX_LOSS_PER_TRADE = 4.0    # Points - hard stop loss


# ============================================================
# Session helpers
# ============================================================

def in_session() -> bool:
    now = datetime.now(ET).time()
    if SESSION_BREAK_START <= now < SESSION_BREAK_END:
        return False
    return True


# ============================================================
# Price tracker for momentum detection
# ============================================================

class PriceTracker:
    """Tracks price ticks over a rolling window to measure momentum."""

    def __init__(self, window_seconds: float):
        self.window = window_seconds
        self.ticks: deque = deque()  # (timestamp, price)

    def add(self, price: float):
        now = time.time()
        self.ticks.append((now, price))
        # Prune old ticks
        cutoff = now - self.window * 2  # Keep 2x window for smoothing
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft()

    def momentum(self) -> float:
        """
        Calculate momentum as price change over the rolling window.
        Positive = price moving up, negative = price moving down.
        """
        if len(self.ticks) < 2:
            return 0.0

        now = time.time()
        cutoff = now - self.window

        # Find oldest tick within window
        old_price = None
        for ts, price in self.ticks:
            if ts >= cutoff:
                old_price = price
                break

        if old_price is None:
            return 0.0

        current_price = self.ticks[-1][1]
        return current_price - old_price

    def speed(self) -> float:
        """Absolute momentum (direction-agnostic speed)."""
        return abs(self.momentum())

    @property
    def current_price(self) -> float | None:
        if not self.ticks:
            return None
        return self.ticks[-1][1]


# ============================================================
# Main Bot
# ============================================================

class MomentumBot:
    def __init__(self, symbol: str, qty: int = 1, tp_dollars: float = 100.0):
        self.symbol = symbol
        self.qty = qty
        self.tp_dollars = tp_dollars

        # Momentum tracker
        self.tracker = PriceTracker(MOMENTUM_WINDOW)

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat
        self.entry_price = 0.0
        self.best_price = 0.0  # Best price since entry (for trailing stop)

        # P&L tracking
        self.session_pnl = 0.0
        self.next_tp_target = tp_dollars
        self.milestone_count = 0
        self.trade_count = 0

        # Timing
        self.last_exit_time = 0.0
        self.was_in_session = False

        # SDK objects
        self.suite = None
        self.ctx = None
        self.running = False

    async def run(self):
        from project_x_py import TradingSuite

        print(f"[BOT] Momentum Scalper Bot")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Momentum: {MOMENTUM_THRESHOLD} pts in {MOMENTUM_WINDOW}s window")
        print(f"[BOT] Trailing Stop: {TRAILING_STOP} pts | Max Loss: {MAX_LOSS_PER_TRADE} pts")
        print(f"[BOT] TP Target: ${self.tp_dollars:.0f} per milestone")
        print(f"[BOT] Session: 18:00 - 16:00 ET")
        print()

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["1min"],
            initial_days=1,
        )

        self.ctx = self.suite[self.symbol]

        print(f"[BOT] Connected to TopstepX")
        print(f"[BOT] Account: {self.suite.client.account_info.name}")
        print(f"[BOT] Contract: {self.ctx.instrument_info.id}")
        print()

        self.running = True
        self.was_in_session = in_session()

        print(f"[BOT] Session active: {self.was_in_session}")
        print(f"[BOT] Collecting price data... (first trade after {MOMENTUM_WINDOW}s)")
        print(f"[BOT] Press Ctrl+C to stop")
        print()

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(0.3)  # Check every 300ms for faster reaction
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _tick(self):
        price = await self.ctx.data.get_current_price()
        if price is None:
            return

        # Always track price for momentum calculation
        self.tracker.add(price)

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

        now_ts = time.time()
        momentum = self.tracker.momentum()

        # === IN POSITION: manage trailing stop, TP, and hard stop ===
        if self.position != 0:
            # Update best price
            if self.position == 1:
                self.best_price = max(self.best_price, price)
            else:
                self.best_price = min(self.best_price, price)

            # Check TP milestone
            unrealized_pnl = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
            total_pnl = self.session_pnl + unrealized_pnl

            if total_pnl >= self.next_tp_target:
                self.milestone_count += 1
                print(f"\n[TP] *** MILESTONE #{self.milestone_count}! Session P&L: ${total_pnl:.2f} ***")
                await self._flatten(price, reason=f"TP_MILESTONE_{self.milestone_count}")
                self.next_tp_target = self.session_pnl + self.tp_dollars
                print(f"[TP] Next target: ${self.next_tp_target:.2f}")
                return

            # Check trailing stop
            if self.position == 1:
                trail_price = self.best_price - TRAILING_STOP
                if price <= trail_price:
                    await self._flatten(price, reason=f"TRAILING_STOP (best={self.best_price:.2f})")
                    return
            else:
                trail_price = self.best_price + TRAILING_STOP
                if price >= trail_price:
                    await self._flatten(price, reason=f"TRAILING_STOP (best={self.best_price:.2f})")
                    return

            # Check hard stop loss
            loss_pts = (price - self.entry_price) * self.position
            if loss_pts <= -MAX_LOSS_PER_TRADE:
                await self._flatten(price, reason=f"HARD_STOP ({loss_pts:+.2f} pts)")
                return

        # === FLAT: look for momentum entry ===
        else:
            # Cooldown after exit
            if (now_ts - self.last_exit_time) < MOMENTUM_COOLDOWN:
                return

            # Need enough data
            if len(self.tracker.ticks) < 5:
                return

            # Check for strong momentum
            if momentum >= MOMENTUM_THRESHOLD:
                await self._enter_long(price, momentum)
            elif momentum <= -MOMENTUM_THRESHOLD:
                await self._enter_short(price, momentum)

    async def _enter_long(self, price: float, momentum: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        self.trade_count += 1
        print(f"\n[{now}] [TRADE #{self.trade_count}] >>> LONG @ {price:.2f} | Momentum: +{momentum:.2f} pts | Session P&L: ${self.session_pnl:.2f}")
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
                print(f"[TRADE] Filled. ID: {response.orderId} | Trail: {price - TRAILING_STOP:.2f} | Stop: {price - MAX_LOSS_PER_TRADE:.2f}")
            else:
                print(f"[TRADE] FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] ERROR: {e}")

    async def _enter_short(self, price: float, momentum: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        self.trade_count += 1
        print(f"\n[{now}] [TRADE #{self.trade_count}] >>> SHORT @ {price:.2f} | Momentum: {momentum:.2f} pts | Session P&L: ${self.session_pnl:.2f}")
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
                print(f"[TRADE] Filled. ID: {response.orderId} | Trail: {price + TRAILING_STOP:.2f} | Stop: {price + MAX_LOSS_PER_TRADE:.2f}")
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
        self.last_exit_time = time.time()

    async def _shutdown(self):
        print("\n[BOT] Shutting down...")
        if self.position != 0:
            price = await self.ctx.data.get_current_price()
            if price:
                await self._flatten(price, reason="SHUTDOWN")

        print(f"\n[BOT] === SESSION SUMMARY ===")
        print(f"[BOT] Total P&L: ${self.session_pnl:.2f}")
        print(f"[BOT] Trades: {self.trade_count}")
        print(f"[BOT] Milestones: {self.milestone_count}")
        print(f"[BOT] ========================")

        if self.suite:
            await self.suite.disconnect()
        print("[BOT] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Momentum Scalper Bot")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    parser.add_argument("--tp", type=float, default=100.0, help="TP milestone in dollars")
    parser.add_argument("--trail", type=float, default=TRAILING_STOP, help="Trailing stop in points")
    parser.add_argument("--threshold", type=float, default=MOMENTUM_THRESHOLD, help="Momentum threshold in points")
    args = parser.parse_args()

    global TRAILING_STOP, MOMENTUM_THRESHOLD
    TRAILING_STOP = args.trail
    MOMENTUM_THRESHOLD = args.threshold

    bot = MomentumBot(
        symbol=args.symbol,
        qty=args.qty,
        tp_dollars=args.tp,
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
