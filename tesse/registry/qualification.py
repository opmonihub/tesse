"""Declarative qualification policy for one exact catalog System version."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from .identity import validate_model_id

_GATE_MAX = {"min_winrate": 1, "min_trades_per_day": 1440, "min_improvement_pp": 100}


def _fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or value.keys() != expected:
        raise ValueError(f"qualification {label} requires exactly {', '.join(sorted(expected))}")


@dataclass(frozen=True, slots=True)
class QualificationScope:
    environment: str
    purpose: str
    assets: tuple[str, ...]

    def authorizes(self, *, asset: str, environment: str, purpose: str) -> bool:
        return (environment, purpose) == (self.environment, self.purpose) and asset in self.assets


@dataclass(frozen=True, slots=True)
class QualificationPolicy:
    system_id: str
    system_version: str
    id: str
    scope: QualificationScope
    gates: Mapping[str, int | float]

    @classmethod
    def from_definition(cls, system_id: str, version: str, value: Any) -> QualificationPolicy:
        _fields(value, {"id", "scope", "gates"}, "policy")
        policy_id = value["id"]
        if not isinstance(policy_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", policy_id):
            raise ValueError("qualification id must be a nonempty portable identifier")

        scope = value["scope"]
        _fields(scope, {"environment", "purpose", "assets"}, "scope")
        if (scope["environment"], scope["purpose"]) != ("TEST", "execution_test"):
            raise ValueError("qualification scope must be TEST for execution_test")
        assets = scope["assets"]
        if not isinstance(assets, list) or not assets:
            raise ValueError("qualification scope assets must be a nonempty list")
        for asset in assets:
            if not isinstance(asset, str):
                raise ValueError("qualification scope contains an invalid asset")
            try:
                validate_model_id(f"{system_id}-{asset}", system_id=system_id, asset=asset)
            except ValueError as exc:
                raise ValueError("qualification scope contains an invalid asset") from exc
        if len(set(assets)) != len(assets):
            raise ValueError("qualification scope contains duplicate assets")

        gates = value["gates"]
        if not isinstance(gates, dict) or not gates:
            raise ValueError("qualification gates must be a nonempty mapping")
        validated = {}
        for gate, threshold in gates.items():
            if gate == "min_settled_trades":
                if type(threshold) is not int or threshold < 1:
                    raise ValueError(f"qualification {gate} requires a positive integer")
                validated[gate] = threshold
            elif gate in _GATE_MAX:
                maximum = _GATE_MAX[gate]
                if type(threshold) not in (int, float) or not 0 <= threshold <= maximum:
                    raise ValueError(f"qualification {gate} requires a number from 0 to {maximum}")
                validated[gate] = float(threshold or 0)
            else:
                raise ValueError(f"unknown qualification gate: {gate}")

        return cls(
            system_id, version, policy_id,
            QualificationScope(scope["environment"], scope["purpose"], tuple(sorted(assets))),
            MappingProxyType(validated),
        )

    @property
    def digest(self) -> str:
        payload = {
            "system_id": self.system_id, "system_version": self.system_version, "id": self.id,
            "scope": {
                "environment": self.scope.environment,
                "purpose": self.scope.purpose,
                "assets": self.scope.assets,
            },
            "gates": dict(self.gates),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    run_id: str
    asset: str
    timeframe: str
    environment: str
    purpose: str
    snapshot_sha256: str
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self):
        if (
            self.start_utc.utcoffset() != timedelta(0)
            or self.end_utc.utcoffset() != timedelta(0)
            or self.end_utc <= self.start_utc
        ):
            raise ValueError("qualification requires a nonempty UTC evaluation window")


@dataclass(frozen=True, slots=True)
class EvaluationResults:
    """Settlement counts derived from an evaluation's verified trade rows."""

    context: EvaluationContext
    wins: int
    losses: int
    ties: int

    def __post_init__(self):
        if any(type(count) is not int or count < 0 for count in (self.wins, self.losses, self.ties)):
            raise ValueError("qualification requires nonnegative integer settlement counts")

    @property
    def settled(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def winrate(self) -> Fraction | None:
        return Fraction(self.wins, self.settled) if self.settled else None


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    threshold: int | float
    observed: int | float | None
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class QualificationDecision:
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates)


def evaluate_qualification(
    policy: QualificationPolicy,
    candidate: EvaluationResults,
    *,
    control: EvaluationResults | None = None,
) -> QualificationDecision:
    """Evaluate all declared gates on checked counts, without consulting the strategy name."""
    context = candidate.context
    if not policy.scope.authorizes(
        asset=context.asset, environment=context.environment, purpose=context.purpose
    ):
        raise ValueError("evaluation is outside qualification scope")
    results = []
    for name, threshold in sorted(policy.gates.items()):
        unavailable = ""
        observed = None
        if name == "min_settled_trades":
            observed = candidate.settled
        elif name == "min_trades_per_day":
            observed = candidate.settled / Fraction(
                (context.end_utc - context.start_utc) // timedelta(microseconds=1),
                timedelta(days=1) // timedelta(microseconds=1),
            )
        elif name == "min_winrate":
            observed = candidate.winrate
            unavailable = "undefined_winrate"
        elif name == "min_improvement_pp":
            if control is None or control.context != context:
                unavailable = "incompatible_control"
            elif candidate.winrate is None or control.winrate is None:
                unavailable = "undefined_winrate"
            else:
                observed = 100 * (candidate.winrate - control.winrate)
        else:
            raise ValueError(f"unknown qualification gate: {name}")
        # Compare exact fractions so a gate at an observed decimal boundary is inclusive.
        passed = observed is not None and observed >= Fraction(str(threshold))
        display = float(observed) if isinstance(observed, Fraction) else observed
        reason = ("passed" if passed else "below_minimum") if observed is not None else unavailable
        results.append(GateResult(name, threshold, display, passed, reason))
    return QualificationDecision(tuple(results))
