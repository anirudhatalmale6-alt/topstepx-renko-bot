"""Renko brick engine with ghost candle and EMA support."""


class RenkoBrick:
    """A single Renko brick."""
    __slots__ = ("open", "close", "is_bullish")

    def __init__(self, open_price: float, close_price: float):
        self.open = open_price
        self.close = close_price
        self.is_bullish = close_price > open_price


class RenkoEngine:
    """
    Builds Renko bricks from price updates with ghost candle tracking.

    Ghost candle: the "forming" brick that hasn't confirmed yet.
    It represents where price currently is relative to the next brick boundary.
    """

    def __init__(self, brick_size: float):
        self.brick_size = brick_size
        self.bricks: list[RenkoBrick] = []
        self._initialized = False
        self.ghost_price = 0.0  # current price (ghost candle tip)

    @property
    def last_brick(self) -> RenkoBrick | None:
        return self.bricks[-1] if self.bricks else None

    @property
    def is_bullish(self) -> bool | None:
        if not self.bricks:
            return None
        return self.bricks[-1].is_bullish

    @property
    def is_bearish(self) -> bool | None:
        if not self.bricks:
            return None
        return not self.bricks[-1].is_bullish

    def update(self, price: float) -> int:
        """
        Feed a new price tick. Returns the number of new bricks created.
        Also updates ghost_price for ghost candle tracking.
        """
        self.ghost_price = price

        if not self._initialized:
            ref = round(price / self.brick_size) * self.brick_size
            self.bricks.append(RenkoBrick(ref - self.brick_size, ref))
            self._initialized = True
            return 1

        new_bricks = 0
        last = self.bricks[-1]

        if last.is_bullish:
            while price >= last.close + self.brick_size:
                new_open = last.close
                new_close = last.close + self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1

            while price <= last.close - 2 * self.brick_size:
                new_open = last.close - self.brick_size
                new_close = last.close - 2 * self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1
        else:
            while price <= last.close - self.brick_size:
                new_open = last.close
                new_close = last.close - self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1

            while price >= last.close + 2 * self.brick_size:
                new_open = last.close + self.brick_size
                new_close = last.close + 2 * self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1

        return new_bricks

    def build_from_ohlc(self, bars: list[dict]) -> None:
        """Build Renko bricks from historical OHLC bars."""
        for bar in bars:
            if bar["close"] >= bar["open"]:
                self.update(bar["low"])
                self.update(bar["high"])
            else:
                self.update(bar["high"])
                self.update(bar["low"])
            self.update(bar["close"])

    def ema(self, period: int) -> float | None:
        """
        Calculate EMA over the last N confirmed brick closes.
        The EMA extends to the ghost candle - it uses confirmed brick
        closes to build the EMA, which the ghost candle then crosses.
        """
        if len(self.bricks) < period:
            return None

        # Calculate EMA from confirmed bricks
        multiplier = 2.0 / (period + 1)
        # Start with SMA of first `period` bricks from the end
        start_idx = max(0, len(self.bricks) - period * 3)  # use more history for accuracy
        closes = [b.close for b in self.bricks[start_idx:]]

        if len(closes) < period:
            return None

        # SMA seed
        ema_val = sum(closes[:period]) / period

        # EMA calculation
        for close in closes[period:]:
            ema_val = (close - ema_val) * multiplier + ema_val

        return ema_val

    def ghost_vs_ema(self, period: int = 9) -> str | None:
        """
        Compare ghost candle price to the EMA.
        Returns 'ABOVE' if ghost > EMA, 'BELOW' if ghost < EMA, None if not enough data.
        """
        ema_val = self.ema(period)
        if ema_val is None:
            return None

        if self.ghost_price > ema_val:
            return "ABOVE"
        elif self.ghost_price < ema_val:
            return "BELOW"
        else:
            return "AT"
