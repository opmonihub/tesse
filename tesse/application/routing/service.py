"""Canonical daily asset-scoped universe and routing decisions."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable, Mapping

from ...models.assets import discover_assets
from ...models.cards import ModelCard, card_hash, validate_runtime_key
from ...runtime.routing import ModelScore, RouterDecision, score_method_window, select_champion

ROUTER_SCORE_V1 = {
    "name": "router_score_v1",
    "window_days": 28,
    "minimum_resolved": 100,
    "minimum_active_days": 10,
    "confidence": "wilson_95",
    "selection_order": ["lower_bound_desc", "resolved_desc", "throughput_desc", "model_id_asc"],
    "incumbent_margin": 0.01,
}


@dataclass(frozen=True, slots=True)
class UniverseResult:
    day: str
    assets: tuple[str, ...]
    exclusions: dict[str, str]


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    asset: str
    day: str
    selected_model_id: str | None
    system_id: str | None
    status: str
    reason: str
    score_config: dict[str, Any]
    scores: tuple[dict[str, Any], ...]
    gates: dict[str, Any]
    identities: dict[str, dict[str, Any]]
    evidence_window: dict[str, Any]
    timestamp: str


def canonical_universe(
    assets: Iterable[Any],
    aliases: Mapping[str, str],
    *,
    available_models: Mapping[str, Iterable[Any]] | None = None,
    utc_day: str | date | None = None,
) -> UniverseResult:
    day = (
        utc_day.isoformat()
        if isinstance(utc_day, date)
        else str(utc_day or datetime.now(UTC).date())
    )
    allowed, skipped = discover_assets(assets, dict(aliases))
    if available_models is not None:
        for symbol in list(allowed):
            models = list(available_models.get(symbol, ()))
            if not models:
                skipped[symbol] = "no_eligible_models"
                allowed.remove(symbol)
    return UniverseResult(day, tuple(allowed), dict(sorted(skipped.items())))


def _gate(
    card: Any,
    *,
    asset: str,
    timeframe: str = "M1",
    cutoff_utc: str | None = None,
    provider: Any = None,
    data_hash: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    gates = {
        "card": card is not None,
        "weights": False,
        "provider_health": False,
        "asset": False,
        "timeframe": False,
        "cutoff": False,
        "dataset_hash": False,
        "runtime_key": False,
        "card_hash": False,
    }
    if not isinstance(card, ModelCard) or not card.is_canonical:
        gates["card"] = False
        return False, gates
    card_asset = getattr(card, "asset", None)
    gates["asset"] = card_asset == asset
    environment = getattr(card, "environment", {}) or {}
    gates["timeframe"] = card.timeframe == timeframe
    gates["weights"] = bool(card.weights_sha256)
    gates["snapshot_hash"] = bool(card.snapshot_sha256)
    runtime_key = card.runtime_key
    try:
        gates["runtime_key"] = bool(validate_runtime_key(runtime_key))
    except ValueError:
        gates["runtime_key"] = False
    gates["card_hash"] = bool(card.card_sha256) and card_hash(card) == card.card_sha256
    gates["cutoff"] = cutoff_utc is None or getattr(card, "fit_cutoff_utc", None) == cutoff_utc
    gates["dataset_hash"] = data_hash is None or environment.get("data_hash") == data_hash
    if provider is not None and getattr(provider, "runtime_key", None) == runtime_key:
        health = getattr(provider, "is_healthy", None)
        try:
            result = health(card) if callable(health) else False
            gates["provider_health"] = not inspect.isawaitable(result) and bool(result)
        except Exception:
            gates["provider_health"] = False
    else:
        # Every new routing decision requires an explicitly resolved provider.
        # Historical cards remain readable through migration readers only.
        gates["provider_health"] = False
    return all(gates.values()), gates


def rank_asset_models(
    asset: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    end: str | datetime,
    cards: Mapping[str, Any] | None = None,
    providers: Mapping[str, Any] | None = None,
    timeframe: str = "M1",
    cutoff_utc: str | None = None,
    data_hash: str | None = None,
    incumbent_id: str | None = None,
    seed_id: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> RoutingDecision:
    cfg = dict(ROUTER_SCORE_V1)
    cfg.update(config or {})
    grouped: dict[str, list[dict]] = {}
    for row in candidates:
        if row.get("asset") != asset:
            continue
        model_id = row.get("model_id")
        if not isinstance(model_id, str):
            continue
        grouped.setdefault(model_id, []).append(dict(row))
    scores: list[ModelScore] = []
    gates_by_model: dict[str, Any] = {}
    identities: dict[str, dict[str, Any]] = {}
    for model_id, rows in grouped.items():
        score = score_method_window(
            rows,
            end,
            window_days=int(cfg["window_days"]),
            minimum_resolved=int(cfg["minimum_resolved"]),
            minimum_active_days=int(cfg["minimum_active_days"]),
        )
        card = (cards or {}).get(model_id)
        provider = (providers or {}).get(model_id)
        ok, gates = _gate(
            card,
            asset=asset,
            timeframe=timeframe,
            cutoff_utc=cutoff_utc,
            provider=provider,
            data_hash=data_hash,
        )
        score = ModelScore(
            score.asset,
            score.model_id,
            score.wins,
            score.resolved,
            score.active_days,
            score.winrate,
            score.lower_bound,
            score.throughput,
            score.eligible and ok,
        )
        scores.append(score)
        gates_by_model[model_id] = gates
        if isinstance(card, ModelCard) and card.is_canonical:
            identities[model_id] = {
                "system_id": card.system_id,
                "system_version": card.system_version,
                "model_version": card.model_version,
                "card_sha256": card.card_sha256,
                "weights_sha256": card.weights_sha256,
                "snapshot_sha256": card.snapshot_sha256,
                "provider": card.runtime_key,
            }
    if not scores:
        decision = RouterDecision(
            asset, incumbent_id or seed_id or "", "abstain_no_candidates", False
        )
    else:
        decision = select_champion(
            scores,
            incumbent_id=incumbent_id,
            seed_id=seed_id or "",
            asset=asset,
            margin=float(cfg["incumbent_margin"]),
        )
    selected = decision.selected_model_id or None
    selected_score = next((score for score in scores if score.model_id == selected), None)
    if selected_score is None or not selected_score.eligible:
        selected = None
    end_at = datetime.fromisoformat(end) if isinstance(end, str) else end
    end_at = end_at.astimezone(UTC)
    return RoutingDecision(
        asset=asset,
        day=end_at.date().isoformat(),
        selected_model_id=selected,
        system_id=identities[selected]["system_id"] if selected else None,
        status="selected" if selected else "abstain",
        reason=decision.reason if selected else "provider_or_selection_gate_failed",
        score_config=cfg,
        scores=tuple(
            asdict(score)
            for score in sorted(
                scores,
                key=lambda s: (
                    -s.lower_bound,
                    -s.resolved,
                    -s.throughput,
                    s.model_id,
                ),
            )
        ),
        gates=gates_by_model,
        identities=identities,
        evidence_window={
            "end": end_at.isoformat(),
            "window_days": cfg["window_days"],
            "start": (end_at - timedelta(days=cfg["window_days"])).isoformat(),
        },
        timestamp=datetime.now(UTC).isoformat(),
    )


def persist_routing_decision(store: Any, decision: RoutingDecision) -> str:
    payload = asdict(decision)
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    store.record_routing_decision(
        {
            **payload,
            "decision_day": decision.day,
            "created_at": decision.timestamp,
            "decision_sha256": identity,
        }
    )
    return identity


def activate_routing_decision(
    store: Any,
    decision: RoutingDecision,
    *,
    boundary: str,
    pointers: Mapping[str, Mapping[str, Any]],
    registry=None,
    provider_resolver=None,
) -> dict[str, Any]:
    """Apply a persisted decision only at an explicit activation boundary."""
    if not boundary:
        raise ValueError("activation boundary is required")
    if decision.status != "selected" or not decision.selected_model_id:
        return {"activated": False, "reason": decision.reason}
    saved = store.routing_decision(decision.asset, decision.day)
    encoded = json.dumps(asdict(decision), sort_keys=True, default=str, separators=(",", ":"))
    if (
        saved is None
        or saved.get("decision_sha256") != hashlib.sha256(encoded.encode()).hexdigest()
    ):
        raise ValueError("routing decision was not persisted")
    accepted = decision.identities.get(decision.selected_model_id, {})
    pointer = pointers.get(decision.asset)
    if pointer is None and hasattr(store, "active_pointer"):
        pointer = store.active_pointer(decision.asset)
    if pointer is None or pointer.get("model_id") != decision.selected_model_id:
        raise ValueError("routing decision pointer identity differs")
    if any(
        pointer.get(field) != accepted.get(identity)
        for field, identity in (
            ("model_version", "model_version"),
            ("card_hash", "card_sha256"),
            ("weights_hash", "weights_sha256"),
            ("provider", "provider"),
        )
    ):
        raise ValueError("routing decision hashes or provider differ from pointer")
    if registry is None or provider_resolver is None:
        raise ValueError("routing activation requires artifact and provider verification")
    from ...runtime.resolver import verified_model_card
    from ...runtime.routing import eligible_for_activation

    with store.transaction():
        metadata = store.get_model_metadata(decision.selected_model_id, pointer["model_version"])
        if metadata is None:
            raise ValueError("model is not registered")
        card = verified_model_card(
            registry, decision.selected_model_id, asset=decision.asset, metadata=metadata
        )
        if (
            not eligible_for_activation(card.created_at_utc, boundary)
            or (datetime.fromisoformat(boundary) <= datetime.fromisoformat(card.fit_cutoff_utc))
            or (
                card.activation_date_utc
                and datetime.fromisoformat(boundary)
                < datetime.fromisoformat(card.activation_date_utc)
            )
        ):
            raise ValueError("routing activation boundary precedes model eligibility")
        provider = provider_resolver.resolve(card)
        if (
            card.snapshot_sha256 != accepted.get("snapshot_sha256")
            or provider is None
            or provider.runtime_key != accepted.get("provider")
        ):
            raise ValueError("routing activation snapshot or provider verification failed")
        result = store.activate_model(
            decision.asset,
            decision.selected_model_id,
            pointer["model_version"],
            card_hash=pointer["card_hash"],
            weights_hash=pointer["weights_hash"],
            provider=pointer["provider"],
            commit_hash=pointer["commit_hash"],
            predecessor_model_id=pointer.get("predecessor_model_id"),
            predecessor_model_version=pointer.get("predecessor_model_version"),
            operation_id=f"routing:{decision.day}:{decision.asset}",
            registry=registry, provider_resolver=provider_resolver,
        )
    return {"activated": True, "boundary": boundary, "pointer": result}
