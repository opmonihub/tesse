"""Maximal WIN/LOSE runs in an already ordered series of executed trades."""

from itertools import groupby
from operator import itemgetter


def trade_streak_metrics(trades) -> dict:
    distributions = {"WIN": {}, "LOSE": {}}
    maxima = {"WIN": 0, "LOSE": 0}
    for status, group in groupby(trades, key=itemgetter("status")):
        length = sum(1 for _ in group)
        distribution = distributions[status]
        key = str(length)
        distribution[key] = distribution.get(key, 0) + 1
        maxima[status] = max(maxima[status], length)
    return dict(
        max_win_streak=maxima["WIN"],
        max_loss_streak=maxima["LOSE"],
        win_streak_distribution=distributions["WIN"],
        loss_streak_distribution=distributions["LOSE"],
    )
