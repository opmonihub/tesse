"""Metrics-first benchmark receipts for methodology comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models.cards import _validate_digest
from .registry.identity import validate_model_id, validate_version


@dataclass(frozen=True, slots=True)
class BenchmarkReceipt:
    benchmark_id: str
    asset: str
    system_id: str
    system_version: str
    model_id: str
    engine_sha256: str
    data_manifest_sha256: str
    rules_sha256: str
    sealed: bool
    metrics: dict[str, Any]


def validate_receipt(receipt: BenchmarkReceipt) -> None:
    if not isinstance(receipt, BenchmarkReceipt):
        raise ValueError("invalid benchmark receipt")
    validate_model_id(receipt.model_id, system_id=receipt.system_id, asset=receipt.asset)
    validate_version(receipt.system_version)
    if not receipt.sealed:
        raise ValueError("benchmark receipt is not sealed")
    for name in ("engine_sha256", "data_manifest_sha256", "rules_sha256"):
        _validate_digest(getattr(receipt, name), name)
    metrics = receipt.metrics
    try:
        wins = int(metrics["wins"])
        resolved = int(metrics["resolved"])
        thirds = metrics["calendar_thirds"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("benchmark metrics are incomplete") from exc
    if resolved < 0 or wins < 0 or wins > resolved:
        raise ValueError("benchmark outcome counts are invalid")
    if not isinstance(thirds, list) or len(thirds) != 3 or any(int(v) < 0 for v in thirds):
        raise ValueError("calendar_thirds must contain three nonnegative counts")
    for name in ("throughput", "active_days", "drawdown", "brier"):
        if name not in metrics or not isinstance(metrics[name], (int, float)):
            raise ValueError(f"benchmark metric missing: {name}")


def publish_scorecard(receipts: list[BenchmarkReceipt], *, claim: str = "") -> list[dict[str, Any]]:
    if "guarantee" in claim.lower() or "garantia" in claim.lower():
        raise ValueError("scorecard cannot contain a profit guarantee")
    seen = set()
    output = []
    for receipt in receipts:
        validate_receipt(receipt)
        key = (receipt.asset, receipt.model_id)
        if key in seen:
            raise ValueError("duplicate asset/model benchmark receipt")
        seen.add(key)
        metrics = receipt.metrics
        resolved = int(metrics["resolved"])
        output.append(
            {
                "benchmark_id": receipt.benchmark_id,
                "asset": receipt.asset,
                "system_id": receipt.system_id,
                "system_version": receipt.system_version,
                "model_id": receipt.model_id,
                "engine_sha256": receipt.engine_sha256,
                "data_manifest_sha256": receipt.data_manifest_sha256,
                "rules_sha256": receipt.rules_sha256,
                "wins": int(metrics["wins"]),
                "resolved": resolved,
                "winrate": int(metrics["wins"]) / resolved if resolved else None,
                "throughput": metrics["throughput"],
                "active_days": int(metrics["active_days"]),
                "calendar_thirds": list(metrics["calendar_thirds"]),
                "drawdown": metrics["drawdown"],
                "brier": metrics["brier"],
            }
        )
    return sorted(output, key=lambda row: (row["asset"], row["model_id"]))
