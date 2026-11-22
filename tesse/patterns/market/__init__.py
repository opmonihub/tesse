"""Boundary sweeps and confirmed breakout/retest observations."""

from collections import deque

from ...indicators.base import continuity, integer_parameter, observation


def failed_breakout(candles, lookback=20):
    integer_parameter("lookback", lookback, 2)
    history = deque(maxlen=lookback)
    for current, count in continuity(candles):
        if count == 1:
            history.clear()
        bias = "none"
        if len(history) == lookback:
            low, high = min(c.low for c in history), max(c.high for c in history)
            if current.low < low and low < current.close < high and current.high <= high:
                bias = "bullish"
            elif current.high > high and low < current.close < high and current.low >= low:
                bias = "bearish"
        yield observation(current, {"bias": bias}, available=count > lookback,
                          reason="warmup" if count <= lookback else None)
        history.append(current)


def breakout_retest(candles, lookback=10, confirmation_bars=3):
    integer_parameter("lookback", lookback, 2)
    integer_parameter("confirmation_bars", confirmation_bars)
    history, pending = deque(maxlen=lookback), None
    for index, (current, count) in enumerate(continuity(candles)):
        if count == 1:
            history.clear()
            pending = None
        bias = "none"
        # Existing setup is confirmed first; at most one confirmation per setup.
        if pending:
            side, origin, level = pending
            if index > origin + confirmation_bars:
                pending = None
            elif side == "bullish":
                if current.low <= level < current.close:
                    bias, pending = side, None
                elif current.close <= level:
                    pending = None
            else:
                if current.high >= level > current.close:
                    bias, pending = side, None
                elif current.close >= level:
                    pending = None
        if len(history) == lookback and bias == "none":
            high, low = max(c.high for c in history), min(c.low for c in history)
            if current.close > high:
                pending = ("bullish", index, high)
            elif current.close < low:
                pending = ("bearish", index, low)
        warm = count >= lookback + 2
        yield observation(current, {"bias": bias}, available=warm,
                          reason="warmup" if not warm else None)
        history.append(current)
