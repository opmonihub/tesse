"""TA-Lib indicators evaluated independently on each uninterrupted M1 segment."""

import math

import numpy as np
import talib

from .base import continuity, observation, valid_volume


def _single(candles, period, function, fields):
    segment = []

    def calculate(rows):
        arrays = [np.asarray([getattr(row, field) for row in rows], dtype=np.float64)
                  for field in fields]
        values = function(*arrays, timeperiod=period)
        for row, value in zip(rows, values, strict=True):
            available = math.isfinite(value)
            yield observation(row, {"value": float(value)} if available else {},
                              available=available, reason=None if available else "warmup")

    for candle, count in continuity(candles):
        if count == 1 and segment:
            yield from calculate(segment)
            segment = []
        segment.append(candle)
    if segment:
        yield from calculate(segment)


def sma(candles, period=20):
    return _single(candles, period, talib.SMA, ("close",))


def rsi(candles, period=14):
    return _single(candles, period, talib.RSI, ("close",))


def atr(candles, period=14):
    return _single(candles, period, talib.ATR, ("high", "low", "close"))


def bollinger(candles, period=20, deviation=2.0):
    return _multiple(candles, talib.BBANDS, ("close",), ("upper", "middle", "lower"),
                     dict(timeperiod=period, nbdevup=deviation, nbdevdn=deviation, matype=0))


def _multiple(candles, function, fields, outputs, parameters):
    segment = []

    def calculate(rows):
        arrays = [np.asarray([getattr(row, field) for row in rows], dtype=np.float64)
                  for field in fields]
        values = function(*arrays, **parameters)
        for row, *items in zip(rows, *values, strict=True):
            available = all(math.isfinite(value) for value in items)
            result = dict(zip(outputs, map(float, items), strict=True)) if available else {}
            yield observation(row, result, available=available,
                              reason=None if available else "warmup")

    for candle, count in continuity(candles):
        if count == 1 and segment:
            yield from calculate(segment)
            segment = []
        segment.append(candle)
    if segment:
        yield from calculate(segment)


def macd(candles, fast_period=12, slow_period=26, signal_period=9):
    if fast_period >= slow_period:
        raise ValueError("fast_period must be smaller than slow_period")
    return _multiple(candles, talib.MACD, ("close",), ("macd", "signal", "histogram"),
                     dict(fastperiod=fast_period, slowperiod=slow_period,
                          signalperiod=signal_period))


def stochastic(candles, fast_k=5, slow_k=3, slow_d=3):
    return _multiple(candles, talib.STOCH, ("high", "low", "close"), ("k", "d"),
                     dict(fastk_period=fast_k, slowk_period=slow_k, slowk_matype=0,
                          slowd_period=slow_d, slowd_matype=0))


def obv(candles):
    candles = tuple(candles)
    if any(not valid_volume(getattr(row, "volume", None)) for row in candles):
        raise ValueError("OBV requires valid explicit volume for every candle")
    segment = []

    def calculate(rows):
        close = np.asarray([row.close for row in rows], dtype=np.float64)
        volume = np.asarray([row.volume for row in rows], dtype=np.float64)
        for row, value in zip(rows, talib.OBV(close, volume), strict=True):
            yield observation(row, {"value": float(value)})

    for candle, count in continuity(candles):
        if count == 1 and segment:
            yield from calculate(segment)
            segment = []
        segment.append(candle)
    if segment:
        yield from calculate(segment)
