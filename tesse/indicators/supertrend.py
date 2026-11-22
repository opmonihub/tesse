"""Causal Supertrend using TA-Lib's Wilder ATR and explicit bullish seed."""

import math

import numpy as np
import talib

from .base import continuity, observation


def supertrend(candles, period=10, multiplier=3.0):
    segment = []

    def calculate(rows):
        arrays = [np.asarray([getattr(row, field) for row in rows], dtype=np.float64)
                  for field in ("high", "low", "close")]
        atr = talib.ATR(*arrays, timeperiod=period)
        upper = lower = previous_close = direction = None
        for candle, volatility in zip(rows, atr, strict=True):
            if not math.isfinite(volatility):
                yield observation(candle, available=False, reason="warmup")
                continue
            middle = (candle.high + candle.low) / 2
            basic_upper = middle + multiplier * volatility
            basic_lower = middle - multiplier * volatility
            if direction is None:
                upper, lower, direction = basic_upper, basic_lower, "bullish"
            else:
                upper = (basic_upper if basic_upper < upper or previous_close > upper else upper)
                lower = (basic_lower if basic_lower > lower or previous_close < lower else lower)
                if direction == "bullish" and candle.close < lower:
                    direction = "bearish"
                elif direction == "bearish" and candle.close > upper:
                    direction = "bullish"
            previous_close = candle.close
            yield observation(candle, {"value": float(lower if direction == "bullish" else upper),
                                       "direction": direction})

    for candle, count in continuity(candles):
        if count == 1 and segment:
            yield from calculate(segment)
            segment = []
        segment.append(candle)
    if segment:
        yield from calculate(segment)
