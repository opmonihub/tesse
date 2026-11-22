"""Canonical financial and technical outcome vocabulary."""
from __future__ import annotations

from typing import Literal

CanonicalOutcome = Literal["WIN", "LOSS", "TIE"]
TechnicalOutcome = Literal["CANCELED", "REFUNDED", "NOT_SENT", "UNKNOWN", "INTENDED", "DISPATCHING", "ACCEPTED"]
CANONICAL_OUTCOMES = frozenset({"WIN", "LOSS", "TIE"})
TECHNICAL_OUTCOMES = frozenset({"CANCELED", "REFUNDED", "NOT_SENT", "UNKNOWN", "INTENDED", "DISPATCHING", "ACCEPTED"})


def normalize_outcome(value: str, *, source: str = "historical") -> str:
    """Normalize broker/historical spelling without collapsing technical states."""
    if not isinstance(value, str):
        raise ValueError("outcome must be a string")
    value = value.upper()
    if value == "LOSE":
        return "LOSS"
    if value in CANONICAL_OUTCOMES or value in TECHNICAL_OUTCOMES:
        return value
    raise ValueError(f"unknown outcome: {value}")


def legacy_outcome(value: str) -> str:
    """Return the old broker spelling for integrations that still require it."""
    value = normalize_outcome(value, source="broker")
    return "LOSE" if value == "LOSS" else value


def is_financial_outcome(value: str) -> bool:
    try:
        return normalize_outcome(value) in CANONICAL_OUTCOMES
    except ValueError:
        return False
