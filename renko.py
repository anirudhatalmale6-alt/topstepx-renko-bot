"""Renko brick engine - builds Renko bricks from OHLC price data."""


class RenkoBrick:
    """A single Renko brick."""
    __slots__ = ("open", "close", "is_bullish")

    def __init__(self, open_price: float, close_price: float):
        self.open = open_price
        self.close = close_price
        self.is_bullish = close_price > open_price


class RenkoEngine:
    """
    Builds Renko bricks from price updates.

    Mimics TradingView's Renko behavior:
    - A new bullish brick forms when price moves UP by brick_size from the last brick's close
    - A new bearish brick forms when price moves DOWN by brick_size from the last brick's close
    - Reversal requires 2x brick_size movement from last brick's close
    """

    def __init__(self, brick_size: float):
        self.brick_size = brick_size
        self.bricks: list[RenkoBrick] = []
        self._initialized = False

    @property
    def last_brick(self) -> RenkoBrick | None:
        return self.bricks[-1] if self.bricks else None

    @property
    def is_bullish(self) -> bool | None:
        """Direction of the last confirmed brick."""
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

        Args:
            price: Current market price

        Returns:
            Number of new bricks formed (0 if none)
        """
        if not self._initialized:
            # First price - create initial reference point
            # Round to nearest brick boundary
            ref = round(price / self.brick_size) * self.brick_size
            self.bricks.append(RenkoBrick(ref - self.brick_size, ref))
            self._initialized = True
            return 1

        new_bricks = 0
        last = self.bricks[-1]

        if last.is_bullish:
            # Currently in uptrend
            # Continue up: price >= last close + brick_size
            while price >= last.close + self.brick_size:
                new_open = last.close
                new_close = last.close + self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1

            # Reversal down: price <= last close - 2*brick_size
            # (reversal needs to go past the open of the last brick by one brick)
            while price <= last.close - 2 * self.brick_size:
                new_open = last.close - self.brick_size
                new_close = last.close - 2 * self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1
        else:
            # Currently in downtrend
            # Continue down: price <= last close - brick_size
            while price <= last.close - self.brick_size:
                new_open = last.close
                new_close = last.close - self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1

            # Reversal up: price >= last close + 2*brick_size
            while price >= last.close + 2 * self.brick_size:
                new_open = last.close + self.brick_size
                new_close = last.close + 2 * self.brick_size
                self.bricks.append(RenkoBrick(new_open, new_close))
                last = self.bricks[-1]
                new_bricks += 1

        return new_bricks

    def build_from_ohlc(self, bars: list[dict]) -> None:
        """
        Build Renko bricks from historical OHLC bars.
        Processes each bar's high and low to simulate price movement.

        Args:
            bars: List of dicts with 'open', 'high', 'low', 'close' keys
        """
        for bar in bars:
            # Process high first if bar is bullish, low first if bearish
            if bar["close"] >= bar["open"]:
                self.update(bar["low"])
                self.update(bar["high"])
            else:
                self.update(bar["high"])
                self.update(bar["low"])
            # Always end on the close
            self.update(bar["close"])


class MultiTimeframeRenko:
    """
    Manages Renko engines for multiple timeframes.
    Checks alignment across all timeframes.
    """

    def __init__(self, brick_size: float, timeframes: list[str] = None):
        if timeframes is None:
            timeframes = ["1m", "3m", "5m", "15m"]
        self.timeframes = timeframes
        self.engines: dict[str, RenkoEngine] = {
            tf: RenkoEngine(brick_size) for tf in timeframes
        }

    def get_engine(self, timeframe: str) -> RenkoEngine:
        return self.engines[timeframe]

    def is_all_bullish(self) -> bool:
        """True if ALL timeframes show a bullish last brick."""
        return all(
            e.is_bullish is True for e in self.engines.values()
        )

    def is_all_bearish(self) -> bool:
        """True if ALL timeframes show a bearish last brick."""
        return all(
            e.is_bearish is True for e in self.engines.values()
        )

    def alignment_status(self) -> dict[str, str]:
        """Get direction of each timeframe."""
        result = {}
        for tf, engine in self.engines.items():
            if engine.is_bullish is True:
                result[tf] = "BULLISH"
            elif engine.is_bearish is True:
                result[tf] = "BEARISH"
            else:
                result[tf] = "NONE"
        return result
