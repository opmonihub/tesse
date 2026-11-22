"""Canonical Wilder DMI/ADX calculation, independent of trading-domain types."""

from dataclasses import asdict, dataclass

from .base import MINUTE, integer_parameter, observation


@dataclass(frozen=True)
class Indicator:
    plus: float
    minus: float
    adx: float


class DMI:
    """First TR/DM uses candle two; arithmetic seeds, then Wilder smoothing.

    The first complete result requires dmi_period + adx_smoothing candles.
    ``params`` supports the existing operational Params object without importing it.
    ``cadence`` is calculation-only support for complete aggregated candles.
    """

    def __init__(self, params=None, *, dmi_period=14, adx_smoothing=14, cadence=MINUTE):
        self.dmi_period = integer_parameter(
            "dmi_period", params.dmi_period if params is not None else dmi_period, 2, 100,
        )
        self.adx_smoothing = integer_parameter(
            "adx_smoothing", params.adx_smoothing if params is not None else adx_smoothing, 2, 100,
        )
        self.cadence = cadence
        self.reset()

    def reset(self):
        self.previous = None
        self.count = self.dx_count = 0
        self.tr = self.plus = self.minus = self.adx = 0.0

    def push(self, candle) -> Indicator | None:
        prev = self.previous
        if prev and candle.timestamp <= prev.timestamp:
            raise ValueError("Candles duplicados ou fora de ordem")
        if prev and candle.timestamp != prev.timestamp + self.cadence:
            self.reset()
            prev = None
        self.previous = candle
        if prev is None:
            return None
        n, a = self.dmi_period, self.adx_smoothing
        tr = max(
            candle.high - candle.low, abs(candle.high - prev.close), abs(candle.low - prev.close),
        )
        up, down = candle.high - prev.high, prev.low - candle.low
        plus, minus = (up if up > down and up > 0 else 0), (down if down > up and down > 0 else 0)
        self.count += 1
        if self.count <= n:
            self.tr += tr / n
            self.plus += plus / n
            self.minus += minus / n
        else:
            self.tr += (tr - self.tr) / n
            self.plus += (plus - self.plus) / n
            self.minus += (minus - self.minus) / n
        if self.count < n:
            return None
        p = 100 * self.plus / self.tr if self.tr else 0
        m = 100 * self.minus / self.tr if self.tr else 0
        dx = 100 * abs(p - m) / (p + m) if p + m else 0
        self.dx_count += 1
        if self.dx_count <= a:
            self.adx += dx / a
        else:
            self.adx += (dx - self.adx) / a
        return Indicator(p, m, self.adx) if self.dx_count >= a else None


def dmi(candles, dmi_period=14, adx_smoothing=14):
    calculator = DMI(dmi_period=dmi_period, adx_smoothing=adx_smoothing)
    for candle in candles:
        value = calculator.push(candle)
        yield observation(candle, asdict(value) if value is not None else {},
                          available=value is not None, reason="warmup" if value is None else None)
