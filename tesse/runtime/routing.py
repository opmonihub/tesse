"""Causal per-asset scoring and daily champion selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ModelScore:
    asset: str
    model_id: str
    wins: int
    resolved: int
    active_days: int
    winrate: float
    lower_bound: float
    throughput: float
    eligible: bool


@dataclass(frozen=True, slots=True)
class RouterDecision:
    asset: str
    selected_model_id: str
    reason: str
    challenger: bool


def _as_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(UTC)


def wilson_lower_bound(wins: int, resolved: int, z: float = 1.96) -> float:
    if resolved < 0 or wins < 0 or wins > resolved:
        raise ValueError("invalid outcome counts")
    if not resolved:
        return 0.0
    p = wins / resolved
    denominator = 1 + z * z / resolved
    center = p + z * z / (2 * resolved)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * resolved)) / resolved)
    return (center - spread) / denominator


def score_method_window(
    predictions: Iterable[dict],
    end: str | datetime,
    *,
    window_days: int = 28,
    minimum_resolved: int = 100,
    minimum_active_days: int = 10,
) -> ModelScore:
    end_at = _as_utc(end)
    start_at = end_at - timedelta(days=window_days)
    rows = list(predictions)
    if not rows:
        raise ValueError("predictions cannot be empty")
    assets = {row.get("asset") for row in rows}
    model_ids = {row.get("model_id") for row in rows}
    if len(assets) != 1 or len(model_ids) != 1:
        raise ValueError("score window must contain one asset and one model")
    asset = next(iter(assets))
    model_id = next(iter(model_ids))
    selected = []
    for row in rows:
        anchor = _as_utc(row["anchor"])
        confirmation = _as_utc(row["confirmation"])
        if not (start_at <= anchor < end_at and anchor <= confirmation < end_at):
            continue
        status = row.get("status")
        if status not in {"WIN", "LOSS", "TIE"}:
            raise ValueError("unknown prediction outcome")
        selected.append((anchor, status, row.get("day") or anchor.date().isoformat()))
    resolved = len(selected)
    wins = sum(status == "WIN" for _, status, _ in selected)
    active_days = len({day for _, _, day in selected})
    lower_bound = wilson_lower_bound(wins, resolved)
    throughput = resolved / window_days
    eligible = resolved >= minimum_resolved and active_days >= minimum_active_days
    return ModelScore(
        asset=asset,
        model_id=model_id,
        wins=wins,
        resolved=resolved,
        active_days=active_days,
        winrate=wins / resolved if resolved else 0.0,
        lower_bound=lower_bound,
        throughput=throughput,
        eligible=eligible,
    )


def select_champion(
    scores: Iterable[ModelScore],
    *,
    incumbent_id: str | None,
    seed_id: str,
    asset: str | None = None,
    margin: float = 0.01,
) -> RouterDecision:
    values = list(scores)
    if not values:
        raise ValueError("scores cannot be empty")
    assets = {value.asset for value in values}
    if len(assets) != 1:
        raise ValueError("scores must belong to one asset")
    score_asset = next(iter(assets))
    if asset is not None and score_asset != asset:
        raise ValueError("asset does not match score window")
    by_id = {value.model_id: value for value in values}
    incumbent = by_id.get(incumbent_id) if incumbent_id else None
    eligible = [value for value in values if value.eligible]
    if not eligible:
        selected = incumbent_id or seed_id
        return RouterDecision(score_asset, selected, "no_eligible_challenger", False)
    best = min(eligible, key=lambda value: (-value.lower_bound, -value.resolved, -value.throughput, value.model_id))
    if incumbent is None:
        return RouterDecision(score_asset, best.model_id, "initial_best_score", best.model_id != seed_id)
    if best.model_id == incumbent.model_id:
        return RouterDecision(score_asset, incumbent.model_id, "incumbent_remains_best", False)
    if best.lower_bound < incumbent.lower_bound + margin:
        return RouterDecision(score_asset, incumbent.model_id, "incumbent_sticky_margin", False)
    return RouterDecision(score_asset, best.model_id, "challenger_beats_incumbent", True)


def daily_challengers(model_ids: Iterable[str], *, limit: int = 3) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    values = sorted(set(model_ids))
    return values[:limit]


def activation_boundary_utc(value: str | datetime | date) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=UTC)
    if isinstance(value, str) and len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)
    return _as_utc(value)


def eligible_for_activation(created_at: str | datetime, activation_day: str | datetime | date) -> bool:
    created = _as_utc(created_at)
    return activation_boundary_utc(activation_day).date() > created.date()
