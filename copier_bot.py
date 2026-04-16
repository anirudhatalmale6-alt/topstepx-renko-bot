"""
TopstepX Signal Copier Bot

Listens to a Telegram channel for trade signals and copies them
to your TopstepX account automatically.

This script contains NO strategy logic - it just copies signals.

Signal format: SIGNAL|KEY|LONG|NQ|25385.50|1
               SIGNAL|KEY|SHORT|NQ|25380.00|1
               SIGNAL|KEY|FLAT|NQ|25382.00|0

Usage:
    export PROJECT_X_USERNAME="your_email"
    export PROJECT_X_API_KEY="your_api_key"
    export PROJECT_X_ACCOUNT_NAME="your_account"
    python copier_bot.py --tg-token YOUR_BOT_TOKEN --tg-chat CHAT_ID --tg-key PASSWORD --symbol NQ --qty 1
"""

import asyncio
import argparse
import signal
import json
import logging
import urllib.request
from datetime import datetime

import pytz

# Suppress noisy SDK background thread errors (position_processor "no running event loop")
logging.getLogger("project_x_py.position_manager.core").setLevel(logging.CRITICAL)
logging.getLogger("project_x_py.realtime.connection_management").setLevel(logging.WARNING)


ET = pytz.timezone("America/New_York")


class SignalCopier:
    def __init__(self, tg_token: str, tg_chat: str, tg_key: str, symbol: str, qty: int = 1):
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.tg_key = tg_key
        self.symbol = symbol
        self.qty = qty

        # Position state
        self.position = 0  # 1=long, -1=short, 0=flat

        # Telegram polling
        self.last_update_id = 0

        # SDK
        self.suite = None
        self.ctx = None
        self.running = False
        self.connected = False
        self.consecutive_errors = 0
        self.MAX_ERRORS_BEFORE_RECONNECT = 3

    async def _connect(self):
        """Connect (or reconnect) to TopstepX."""
        from project_x_py import TradingSuite

        if self.suite:
            try:
                await self.suite.disconnect()
            except Exception:
                pass
            self.suite = None
            self.ctx = None

        self.suite = await TradingSuite.create(
            instruments=self.symbol,
            timeframes=["1min"],
            initial_days=0,
        )
        self.ctx = self.suite[self.symbol]
        self.connected = True
        self.consecutive_errors = 0

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [COPIER] Connected to TopstepX")
        print(f"[COPIER] Account: {self.suite.client.account_info.name}")
        print(f"[COPIER] Contract: {self.ctx.instrument_info.id}")

    async def run(self):
        print(f"[COPIER] Signal Copier Bot")
        print(f"[COPIER] Symbol: {self.symbol}, Qty: {self.qty}")
        print(f"[COPIER] Listening to Telegram for signals...")
        print()

        await self._connect()

        print(f"[COPIER] Press Ctrl+C to stop")
        print()

        self.running = True

        try:
            while self.running:
                await self._poll_signals()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _poll_signals(self):
        """Poll Telegram for new messages containing signals."""
        try:
            url = f"https://api.telegram.org/bot{self.tg_token}/getUpdates?offset={self.last_update_id + 1}&timeout=1"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))

            if not data.get("ok"):
                return

            for update in data.get("result", []):
                self.last_update_id = update["update_id"]
                msg = update.get("message", {}) or update.get("channel_post", {})
                text = msg.get("text", "")

                if text.startswith("SIGNAL|"):
                    await self._process_signal(text)

        except Exception as e:
            # Timeout or network error - just retry next loop
            if "timed out" not in str(e).lower():
                print(f"[COPIER] Poll error: {e}")

    async def _process_signal(self, signal_text: str):
        """Parse and execute a trade signal."""
        now = datetime.now(ET).strftime("%H:%M:%S")
        parts = signal_text.strip().split("|")
        # Format: SIGNAL|KEY|DIRECTION|SYMBOL|PRICE|QTY

        if len(parts) < 5:
            print(f"[{now}] [COPIER] Bad signal: {signal_text}")
            return

        sig_key = parts[1]     # passkey
        direction = parts[2]   # LONG, SHORT, or FLAT
        sig_symbol = parts[3]  # NQ
        sig_price = parts[4]   # reference price from sender

        if sig_key != self.tg_key:
            print(f"[{now}] [COPIER] Invalid key - signal rejected")
            return

        print(f"[{now}] [COPIER] Received signal: {direction} {sig_symbol} @ {sig_price}")

        if direction == "FLAT":
            if self.position != 0:
                await self._flatten(f"SIGNAL_FLAT @ {sig_price}")
            return

        if direction == "LONG":
            if self.position == -1:
                await self._flatten("FLIP_TO_LONG")
            if self.position <= 0:
                await self._enter_long()

        elif direction == "SHORT":
            if self.position == 1:
                await self._flatten("FLIP_TO_SHORT")
            if self.position >= 0:
                await self._enter_short()

    async def _try_reconnect(self):
        """Attempt to reconnect to TopstepX."""
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [COPIER] Connection lost - reconnecting...")
        self.connected = False
        try:
            await self._connect()
            print(f"[{now}] [COPIER] Reconnected successfully!")
            return True
        except Exception as e:
            print(f"[{now}] [COPIER] Reconnect failed: {e}")
            print(f"[{now}] [COPIER] Will retry on next signal...")
            return False

    async def _enter_long(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [TRADE] >>> ENTERING LONG")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=0,
                size=self.qty,
            )
            if response.success:
                self.position = 1
                self.consecutive_errors = 0
                print(f"[TRADE] Filled. ID: {response.orderId}")
            else:
                print(f"[TRADE] FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] ERROR: {e}")
            self.consecutive_errors += 1
            if self.consecutive_errors >= self.MAX_ERRORS_BEFORE_RECONNECT:
                if await self._try_reconnect():
                    await self._enter_long()  # Retry after reconnect

    async def _enter_short(self):
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [TRADE] >>> ENTERING SHORT")
        try:
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=1,
                size=self.qty,
            )
            if response.success:
                self.position = -1
                self.consecutive_errors = 0
                print(f"[TRADE] Filled. ID: {response.orderId}")
            else:
                print(f"[TRADE] FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] ERROR: {e}")
            self.consecutive_errors += 1
            if self.consecutive_errors >= self.MAX_ERRORS_BEFORE_RECONNECT:
                if await self._try_reconnect():
                    await self._enter_short()  # Retry after reconnect

    async def _flatten(self, reason: str = ""):
        direction = "LONG" if self.position == 1 else "SHORT"
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] [TRADE] <<< EXITING {direction} | {reason}")
        try:
            close_side = 1 if self.position == 1 else 0
            response = await self.ctx.orders.place_market_order(
                contract_id=self.ctx.instrument_info.id,
                side=close_side,
                size=self.qty,
            )
            if response.success:
                print(f"[TRADE] Closed. ID: {response.orderId}")
                self.consecutive_errors = 0
            else:
                print(f"[TRADE] CLOSE FAILED: {response.errorMessage}")
        except Exception as e:
            print(f"[TRADE] Close ERROR: {e}")
            self.consecutive_errors += 1
            if self.consecutive_errors >= self.MAX_ERRORS_BEFORE_RECONNECT:
                if await self._try_reconnect():
                    await self._flatten(reason)  # Retry after reconnect

        self.position = 0

    async def _shutdown(self):
        print("\n[COPIER] Shutting down...")
        if self.position != 0:
            await self._flatten("SHUTDOWN")

        if self.suite:
            await self.suite.disconnect()
        print("[COPIER] Disconnected. Goodbye!")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="TopstepX Signal Copier")
    parser.add_argument("--tg-token", required=True, help="Telegram bot token")
    parser.add_argument("--tg-chat", required=True, help="Telegram chat/channel ID")
    parser.add_argument("--tg-key", required=True, help="Passkey to authenticate signals")
    parser.add_argument("--symbol", default="NQ", help="Contract symbol")
    parser.add_argument("--qty", type=int, default=1, help="Order quantity")
    args = parser.parse_args()

    bot = SignalCopier(
        tg_token=args.tg_token,
        tg_chat=args.tg_chat,
        tg_key=args.tg_key,
        symbol=args.symbol,
        qty=args.qty,
    )

    # Windows compatibility: use SelectorEventLoop instead of ProactorEventLoop
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()

    def handle_sig(sig, frame):
        bot.running = False
        print("\n[COPIER] Ctrl+C received, stopping...")

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
