"""EMA seeded by the first close, matching the original research calculation."""

from .base import MINUTE, integer_parameter, observation


class EMA:
    """First-close seed, alpha=2/(period+1), available after period M1 candles."""

    def __init__(self, period=20):
        self.period = integer_parameter("period", period)
        self.reset()

    def reset(self):
        self.previous = self.value = None
        self.count = 0

    def push(self, candle):
        if self.previous is not None and candle.timestamp <= self.previous:
            raise ValueError("Candles duplicados ou fora de ordem")
        if self.previous is not None and candle.timestamp != self.previous + MINUTE:
            self.reset()
        self.previous = candle.timestamp
        self.count += 1
        self.value = (candle.close if self.value is None else
                      self.value + 2 / (self.period + 1) * (candle.close - self.value))
        return self.value if self.count >= self.period else None


def ema(candles, period=20):
    calculator = EMA(period)
    for candle in candles:
        value = calculator.push(candle)
        yield observation(candle, {"value": value} if value is not None else {},
                          available=value is not None, reason="warmup" if value is None else None)
