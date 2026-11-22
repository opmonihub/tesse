"""Immutable, timed observations shared by standalone analysis components."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from numbers import Real
from types import MappingProxyType

MINUTE = timedelta(minutes=1)


def valid_volume(value):
    return (isinstance(value, Real) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


@dataclass(frozen=True, slots=True)
class Observation:
    """A closed M1 candle's identity and the instant its result can be used."""

    candle_at: datetime
    available_at: datetime
    values: Mapping
    available: bool = True
    reason: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

def observation(candle, values=None, *, available=True, reason=None):
    return Observation(
        candle.timestamp, candle.timestamp + MINUTE, values or {}, available, reason,
    )


def continuity(candles):
    """Yield each candle and its contiguous segment length; reject reordered input."""
    previous, count = None, 0
    for candle in candles:
        if previous is not None and candle.timestamp <= previous:
            raise ValueError("Candles duplicados ou fora de ordem")
        count = count + 1 if previous is not None and candle.timestamp == previous + MINUTE else 1
        yield candle, count
        previous = candle.timestamp


def integer_parameter(name, value, minimum=1, maximum=10000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} deve ser inteiro entre {minimum} e {maximum}")
    return value
