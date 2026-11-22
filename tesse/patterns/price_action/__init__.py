"""Inside-bar breakout and confirmed Hikkake structures."""

from collections import deque

from ...indicators.base import continuity, integer_parameter, observation


def inside_breakout(candles):
    history = deque(maxlen=2)
    for current, count in continuity(candles):
        if count == 1:
            history.clear()
        bias = "none"
        if len(history) == 2:
            outer, inner = history
            if inner.high < outer.high and inner.low > outer.low:
                if current.close > outer.high:
                    bias = "bullish"
                elif current.close < outer.low:
                    bias = "bearish"
        yield observation(current, {"bias": bias}, available=count >= 3,
                          reason="warmup" if count < 3 else None)
        history.append(current)


def hikkake(candles, confirmation_bars=3):
    integer_parameter("confirmation_bars", confirmation_bars)
    history, pending = deque(maxlen=2), None
    for index, (current, count) in enumerate(continuity(candles)):
        if count == 1:
            history.clear()
            pending = None
        bias, new_pattern = "none", None
        if len(history) == 2:
            outer, inner = history
            if inner.high < outer.high and inner.low > outer.low:
                if current.high < inner.high and current.low < inner.low:
                    new_pattern = ("bullish", index, inner.high)
                elif current.high > inner.high and current.low > inner.low:
                    new_pattern = ("bearish", index, inner.low)
        if new_pattern:
            pending = new_pattern
        elif pending:
            side, origin, level = pending
            if index > origin + confirmation_bars:
                pending = None
            elif current.close > level if side == "bullish" else current.close < level:
                if count >= 6:
                    bias = side
                pending = None
        yield observation(current, {"bias": bias}, available=count >= 6,
                          reason="warmup" if count < 6 else None)
        history.append(current)
