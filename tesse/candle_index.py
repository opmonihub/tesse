"""Immutable temporal lookup shared only within one asset evaluation."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CandleIndex(Mapping):
    """A timestamp-to-candle view backed by one timestamp-to-position map."""

    rows: tuple
    by_time: Mapping
    segment_starts: tuple

    @classmethod
    def build(cls, rows):
        rows = tuple(rows)
        by_time, segment_starts = {}, []
        previous, segment_start = None, 0
        minute = timedelta(minutes=1)
        for position, row in enumerate(rows):
            timestamp = row.timestamp
            if previous is not None:
                if timestamp <= previous:
                    raise ValueError("Candles have duplicate or decreasing timestamps")
                if timestamp != previous + minute:
                    segment_start = position
            by_time[timestamp] = position
            segment_starts.append(segment_start)
            previous = timestamp
        return cls(rows, MappingProxyType(by_time), tuple(segment_starts))

    def __getitem__(self, timestamp):
        return self.rows[self.by_time[timestamp]]

    def get(self, timestamp, default=None):
        position = self.by_time.get(timestamp)
        return default if position is None else self.rows[position]

    def __contains__(self, timestamp):
        return timestamp in self.by_time

    def __iter__(self):
        return iter(self.by_time)

    def __len__(self):
        return len(self.rows)
