"""
TopstepX Signal Copier (Windows)
Listens for trading signals via ntfy.sh and copies trades to your TopstepX account.

Usage:
    set PROJECT_X_USERNAME=your-email@example.com
    set PROJECT_X_API_KEY=your-api-key
    set PROJECT_X_ACCOUNT_NAME=PRAC-V2-xxxxx-xxxxx
    python signal_copier.py --ntfy-topic nqsig-xxxxx --symbol NQ --qty 1
"""

import asyncio
import argparse
import json
import signal
import time
import urllib.request
import urllib.error
from datetime import datetime, time as dtime

import pytz

ET = pytz.timezone("America/New_York")

POINT_VALUES = {
    "NQ": 20.0, "ES": 50.0, "MNQ": 2.0, "MES": 5.0, "YM": 5.0, "RTY": 10.0,
}


class SignalCopier:
    def __init__(self, ntfy_topic: str, symbol: str, qty: int):
        self.ntfy_topic = ntfy_topic
        self.symbol = symbol.upper()
        self.qty = qty
        self.point_value = POINT_VALUES.get(self.symbol, 20.0)

        self.position = 0  # 0=flat, 1=long, -1=short
        self.entry_price = 0.0
        self.live_pnl = 0.0

        self.suite = None
        self.ctx = None
        self.running = True

    async def connect(self):
        from project_x_py import TradingSuite
        print(f"[COPIER] Connecting to TopstepX...")
        self.suite = await TradingSuite.create(
            instruments=[self.symbol],
            timeframes=["1sec", "15min"],
            initial_days=1,
        )
        self.ctx = self.suite[self.symbol]
        print(f"[COPIER] Connected!")
        print(f"[COPIER] Symbol: {self.symbol} | Qty: {self.qty}")
        print(f"[COPIER] Listening to ntfy topic: {self.ntfy_topic}")
        print(f"[COPIER] Waiting for signals...\n")

        # Clear any orphan position
        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=5.0)
        except Exception:
            pass

    async def handle_signal(self, signal_type: str, price: float):
        now = datetime.now(ET).strftime("%H:%M:%S")

        if signal_type == "LONG":
            if self.position == -1:
                await self._close_position(price, "FLIP_TO_LONG")
            if self.position == 0:
                print(f"[{now}] >>> ENTERING LONG @ {price:.2f}")
                try:
                    response = await asyncio.wait_for(
                        self.ctx.orders.place_market_order(
                            contract_id=self.ctx.instrument_info.id,
                            side=0, size=self.qty),
                        timeout=15.0)
                    if response.success:
                        self.position = 1
                        self.entry_price = price
                        print(f"[{now}] LONG filled. ID: {response.orderId}")
                    else:
                        print(f"[{now}] LONG FAILED: {response.errorMessage}")
                except Exception as e:
                    print(f"[{now}] LONG ERROR: {e}")

        elif signal_type == "SHORT":
            if self.position == 1:
                await self._close_position(price, "FLIP_TO_SHORT")
            if self.position == 0:
                print(f"[{now}] >>> ENTERING SHORT @ {price:.2f}")
                try:
                    response = await asyncio.wait_for(
                        self.ctx.orders.place_market_order(
                            contract_id=self.ctx.instrument_info.id,
                            side=1, size=self.qty),
                        timeout=15.0)
                    if response.success:
                        self.position = -1
                        self.entry_price = price
                        print(f"[{now}] SHORT filled. ID: {response.orderId}")
                    else:
                        print(f"[{now}] SHORT FAILED: {response.errorMessage}")
                except Exception as e:
                    print(f"[{now}] SHORT ERROR: {e}")

        elif signal_type == "FLAT":
            if self.position != 0:
                await self._close_position(price, "SIGNAL_FLAT")

    async def _close_position(self, price: float, reason: str):
        direction = "LONG" if self.position == 1 else "SHORT"
        trade_pnl = (price - self.entry_price) * self.position * self.point_value * self.qty
        self.live_pnl += trade_pnl
        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] <<< CLOSING {direction} @ {price:.2f} | Trade: ${trade_pnl:+.2f} | P&L: ${self.live_pnl:.2f} | {reason}")

        self.position = 0
        self.entry_price = 0.0

        try:
            await asyncio.wait_for(
                self.ctx.positions.close_position_direct(
                    contract_id=self.ctx.instrument_info.id),
                timeout=5.0)
            print(f"[{now}] Position closed")
        except Exception:
            try:
                close_side = 1 if direction == "LONG" else 0
                await asyncio.wait_for(
                    self.ctx.orders.place_market_order(
                        contract_id=self.ctx.instrument_info.id,
                        side=close_side, size=self.qty),
                    timeout=15.0)
                print(f"[{now}] Position closed (fallback)")
            except Exception as e:
                print(f"[{now}] CLOSE ERROR: {e}")

    async def listen_ntfy(self):
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                url = f"https://ntfy.sh/{self.ntfy_topic}/json?poll=1&since=10s"
                req = urllib.request.Request(url)

                def fetch():
                    try:
                        resp = urllib.request.urlopen(req, timeout=15)
                        return resp.read().decode("utf-8")
                    except Exception:
                        return ""

                data = await loop.run_in_executor(None, fetch)

                if data:
                    for line in data.strip().split("\n"):
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                            if msg.get("event") == "message":
                                text = msg.get("message", "")
                                await self._process_signal(text)
                        except json.JSONDecodeError:
                            pass

            except Exception as e:
                print(f"[ntfy] Error: {e}")

            await asyncio.sleep(5)

    async def _process_signal(self, text: str):
        parts = text.split("|")
        if len(parts) < 2:
            return

        signal_type = None
        price = 0.0

        for part in parts:
            part = part.strip().upper()
            if part in ("LONG", "SHORT", "FLAT"):
                signal_type = part
            try:
                val = float(part)
                if val > 1000:
                    price = val
            except ValueError:
                pass

        if signal_type is None:
            return

        if price == 0.0 and self.ctx:
            try:
                price = await self.ctx.data.get_current_price() or 0.0
            except Exception:
                pass

        now = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[{now}] SIGNAL RECEIVED: {signal_type} @ {price:.2f}")
        await self.handle_signal(signal_type, price)

    async def run(self):
        await self.connect()
        await self.listen_ntfy()

        if self.suite:
            try:
                await self.suite.disconnect()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="TopstepX Signal Copier")
    parser.add_argument("--ntfy-topic", required=True, help="ntfy.sh topic to listen for signals")
    parser.add_argument("--symbol", default="NQ", help="Symbol to trade (default: NQ)")
    parser.add_argument("--qty", type=int, default=1, help="Quantity (default: 1)")
    args = parser.parse_args()

    copier = SignalCopier(
        ntfy_topic=args.ntfy_topic,
        symbol=args.symbol,
        qty=args.qty,
    )

    def handle_sig(sig, frame):
        copier.running = False
        print("\n[COPIER] Shutting down...")

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    print("=" * 50)
    print("  TopstepX Signal Copier")
    print("=" * 50)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(copier.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
