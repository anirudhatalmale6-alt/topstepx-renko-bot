"""
TopstepX Realtime Candle Color Strategy Bot

Strategy: Trades based on the current 15-minute candle color.
- Green candle (close > open) = LONG
- Red candle (close < open) = SHORT
- Doji (body < 1% of range) = ignored

Optional: Sends trade signals to Telegram for copy-trading.

Session: 9:00 AM - 4:00 PM ET

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python color_bot.py --symbol NQ --qty 1 --tp 99999

    # With Telegram signals:
    python color_bot.py --symbol NQ --qty 1 --tp 99999 --tg-token YOUR_BOT_TOKEN --tg-chat YOUR_CHAT_ID --tg-key mypassword
"""

import asyncio
import argparse
import signal
import json
import urllib.request
from datetime import datetime, time as dtime

import pytz


# ============================================================
# Telegram helper
# ============================================================

def send_telegram(token: str, chat_id: str, message: str):
    """Send a message to Telegram channel. Fire and forget."""
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[TG] Send failed: {e}")


# ============================================================
# Configuration
# ============================================================

ET = pytz.timezone("America/New_York")

SESSION_START = dtime(9, 0)
SESSION_END = dtime(16, 0)

TIMEFRAME = "15min"
DOJI_THRESHOLD = 0.01

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
    def __init__(self, symbol: str, qty: int = 1, tp_dollars: float = 100.0,
                 tg_token: str = "", tg_chat: str = "", tg_key: str = ""):
        self.symbol = symbol
        self.qty = qty
        self.tp_dollars = tp_dollars
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_key = tg_key

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat
        self.entry_price = 0.0

        # P&L tracking
        self.session_pnl = 0.0  # Total session P&L in dollars
        self.next_tp_target = tp_dollars  # First target is +$100
        self.milestone_count = 0
        self.tp_paused = False  # Paused after hitting TP, waiting for next candle

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
        from project_x_py import TradingSuite

        tp_pts = self.tp_dollars / (POINT_VALUE * self.qty)
        print(f"[BOT] Realtime Candle Color Bot + ${self.tp_dollars:.0f} TP Milestones")
        print(f"[BOT] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[BOT] Timeframe: {TIMEFRAME}")
        print(f"[BOT] TP Target: ${self.tp_dollars:.0f} ({tp_pts:.2f} pts per milestone)")
        print(f"[BOT] Session: 09:00 - 16:00 ET")
        if self.tg_token and self.tg_chat:
            print(f"[BOT] Telegram signals: ENABLED")
        else:
            print(f"[BOT] Telegram signals: OFF (use --tg-token and --tg-chat to enable)")
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
                self.tp_paused = False  # Resume trading on new candle
                now = datetime.now(ET).strftime("%H:%M:%S")
                print(f"[{now}] New {TIMEFRAME} candle opened @ {candle_open:.2f}")

        if self.current_candle_open is None:
            return

        # Calculate candle color
        body = abs(price - self.current_candle_open)
        is_doji = body < 0.01  # Less than 1 cent movement = doji
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

        # Check TP milestone while in position
        if self.position != 0:
            unrealized_pnl = (price - self.entry_price) * self.position * POINT_VALUE * self.qty
            total_pnl = self.session_pnl + unrealized_pnl

            if total_pnl >= self.next_tp_target:
                self.milestone_count += 1
                print(f"\n[TP] *** MILESTONE #{self.milestone_count} HIT! Session P&L: ${total_pnl:.2f} (target was ${self.next_tp_target:.2f}) ***")
                await self._flatten(price, reason=f"TP_MILESTONE_{self.milestone_count}")
                self.next_tp_target = self.session_pnl + self.tp_dollars
                self.tp_paused = True  # Wait for next candle before re-entering
                print(f"[TP] Next target: ${self.next_tp_target:.2f}")
                return

        # Don't enter new trades if paused after TP
        if self.tp_paused:
            return

        # Trading logic based on candle color
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
        print(f"\n[{now}] [TRADE] >>> ENTERING LONG @ {price:.2f} (candle GREEN) | Session P&L: ${self.session_pnl:.2f} | Target: ${self.next_tp_target:.2f}")
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
                send_telegram(self.tg_token, self.tg_chat,
                              f"SIGNAL|{self.tg_key}|LONG|{self.symbol}|{price}|{self.qty}")
            else:
                print(f"[TRADE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Order ERROR: {e}")

    async def _enter_short(self, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [TRADE] >>> ENTERING SHORT @ {price:.2f} (candle RED) | Session P&L: ${self.session_pnl:.2f} | Target: ${self.next_tp_target:.2f}")
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
                send_telegram(self.tg_token, self.tg_chat,
                              f"SIGNAL|{self.tg_key}|SHORT|{self.symbol}|{price}|{self.qty}")
            else:
                print(f"[TRADE] Order FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Order ERROR: {e}")

    async def _flatten(self, price: float, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        trade_pnl_pts = (price - self.entry_price) * self.position
        trade_pnl_dollars = trade_pnl_pts * POINT_VALUE * self.qty
        self.session_pnl += trade_pnl_dollars

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"\n[{now}] [TRADE] <<< EXITING {direction} @ {price:.2f} | Trade P&L: ${trade_pnl_dollars:+.2f} | Session P&L: ${self.session_pnl:.2f} | Reason: {reason}")
        send_telegram(self.tg_token, self.tg_chat,
                      f"SIGNAL|{self.tg_key}|FLAT|{self.symbol}|{price}|0")

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

        print(f"\n[BOT] === SESSION SUMMARY ===")
        print(f"[BOT] Total P&L: ${self.session_pnl:.2f}")
        print(f"[BOT] Milestones hit: {self.milestone_count}")
        print(f"[BOT] ========================")

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
    parser.add_argument("--tp", type=float, default=100.0, help="Take profit target in dollars per milestone")
    parser.add_argument("--tg-token", default="", help="Telegram bot token for signal broadcasting")
    parser.add_argument("--tg-chat", default="", help="Telegram chat/channel ID for signals")
    parser.add_argument("--tg-key", default="", help="Passkey for signal authentication (friends need this to copy)")
    args = parser.parse_args()

    bot = CandleColorBot(
        symbol=args.symbol,
        qty=args.qty,
        tp_dollars=args.tp,
        tg_token=args.tg_token,
        tg_chat=args.tg_chat,
        tg_key=args.tg_key,
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
