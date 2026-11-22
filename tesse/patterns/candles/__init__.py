"""Candlestick shape observations without strategy decisions."""

import math

import numpy as np
import talib
from talib import abstract

from ...indicators.base import continuity, observation


def engulfing(candles):
    previous = None
    for current, count in continuity(candles):
        bias = "none"
        if count > 1:
            opposite = (current.close - current.open) * (previous.close - previous.open) < 0
            if opposite:
                if (current.close > current.open and current.open <= previous.close
                        and current.close >= previous.open
                        and (current.open < previous.close or current.close > previous.open)):
                    bias = "bullish"
                elif (current.close < current.open and current.open >= previous.close
                        and current.close <= previous.open
                        and (current.open > previous.close or current.close < previous.open)):
                    bias = "bearish"
        yield observation(current, {"bias": bias}, available=count >= 2,
                          reason="warmup" if count < 2 else None)
        previous = current


def pin_reversal(candles, wick_body_ratio=2.0, opposite_wick_body_ratio=1.0):
    for name, value in (("wick_body_ratio", wick_body_ratio),
                        ("opposite_wick_body_ratio", opposite_wick_body_ratio)):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} deve ser numero finito nao negativo")
    for current, _ in continuity(candles):
        body = abs(current.close - current.open)
        lower = min(current.open, current.close) - current.low
        upper = current.high - max(current.open, current.close)
        bias = "none"
        if body > 0:
            if (lower >= wick_body_ratio * body and upper <= opposite_wick_body_ratio * body
                    and current.close > current.open):
                bias = "bullish"
            elif (upper >= wick_body_ratio * body and lower <= opposite_wick_body_ratio * body
                    and current.close < current.open):
                bias = "bearish"
        yield observation(current, {"bias": bias})


def candle_pattern(candles, name):
    segment = []
    lookback = abstract.Function(name).lookback
    function = getattr(talib, name)

    def calculate(rows):
        arrays = [np.asarray([getattr(row, field) for row in rows], dtype=np.float64)
                  for field in ("open", "high", "low", "close")]
        for index, (row, value) in enumerate(zip(rows, function(*arrays), strict=True)):
            available = index >= lookback
            yield observation(row, {"detected": bool(value), "score": int(value)} if available else {},
                              available=available, reason=None if available else "warmup")

    for candle, count in continuity(candles):
        if count == 1 and segment:
            yield from calculate(segment)
            segment = []
        segment.append(candle)
    if segment:
        yield from calculate(segment)
