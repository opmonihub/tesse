"""Canonical, validated identities for systems and asset-scoped models."""
from __future__ import annotations

import re
from dataclasses import dataclass

SYSTEM_ID_RE = re.compile(r"^(?P<family>[A-Z][A-Z0-9]*)-(?P<behavior>[A-Z][A-Z0-9]*)-(?P<timeframe>M[0-9]+|H[0-9]+|D[0-9]+|W[0-9]+)-(?P<number>[0-9]{3,})$")
MODEL_ID_RE = re.compile(r"^(?P<system>.+)-(?P<asset>[A-Z][A-Z0-9]*)$")
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
DATE_VERSION_RE = re.compile(r"^[0-9]{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12][0-9]|3[01])$")

# Vocabulary is intentionally small and extendable through an explicit review.
FAMILIES = frozenset({"RANK", "TREND", "MEAN", "BREAKOUT", "MOMENTUM", "PATTERN", "HYBRID"})
BEHAVIORS = frozenset({"EDGE", "PULLBACK", "CROSS", "RETEST", "REVERSAL", "RANGE", "HGB", "DMI"})
TIMEFRAMES = frozenset({"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"})


def validate_system_id(value: str) -> str:
    if not isinstance(value, str) or not SYSTEM_ID_RE.fullmatch(value):
        raise ValueError("system_id must match <FAMILY>-<BEHAVIOR>-<TIMEFRAME>-<NUMBER>")
    match = SYSTEM_ID_RE.fullmatch(value)
    assert match is not None
    if match.group("family") not in FAMILIES:
        raise ValueError(f"unknown system family: {match.group('family')}")
    if match.group("behavior") not in BEHAVIORS:
        raise ValueError(f"unknown system behavior: {match.group('behavior')}")
    if match.group("timeframe") not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe: {match.group('timeframe')}")
    return value


def validate_model_id(value: str, *, system_id: str | None = None, asset: str | None = None) -> str:
    if not isinstance(value, str) or not MODEL_ID_RE.fullmatch(value):
        raise ValueError("model_id must append an uppercase asset to system_id")
    match = MODEL_ID_RE.fullmatch(value)
    assert match is not None
    model_system, model_asset = match.group("system"), match.group("asset")
    validate_system_id(model_system)
    if not model_asset.isalnum() or model_asset != model_asset.upper():
        raise ValueError("model asset must be uppercase alphanumeric")
    if system_id is not None and model_system != validate_system_id(system_id):
        raise ValueError("model_id system does not match system_id")
    if asset is not None and model_asset != asset:
        raise ValueError("model_id asset does not match asset")
    return value


def validate_version(value: str) -> str:
    if not isinstance(value, str) or not (SEMVER_RE.fullmatch(value) or DATE_VERSION_RE.fullmatch(value)):
        raise ValueError("version must be semver or YYYY.MM.DD")
    return value


@dataclass(frozen=True, slots=True)
class SystemIdentity:
    system_id: str
    system_version: str

    def __post_init__(self):
        validate_system_id(self.system_id)
        validate_version(self.system_version)


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    system_id: str
    system_version: str
    model_id: str
    model_version: str
    asset: str
    timeframe: str

    def __post_init__(self):
        validate_system_id(self.system_id)
        validate_version(self.system_version)
        validate_model_id(self.model_id, system_id=self.system_id, asset=self.asset)
        validate_version(self.model_version)
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe: {self.timeframe}")
