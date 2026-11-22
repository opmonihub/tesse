"""Commit model activation in the Store and repair its filesystem projection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..demo_dmi import ASSET as DEMO_ASSET
from ..demo_dmi import RUNTIME_KEY as DEMO_PROVIDER
from ..demo_dmi import SYSTEM_ID as DEMO_SYSTEM
from ..demo_dmi import VERSION as DEMO_VERSION
from ..models.cards import ModelCard, card_hash
from ..registry.models import ModelArtifactRegistry
from ..state import Store, dumps
from .active_snapshot import write_active_snapshot
from .resolver import ActiveModelResolver, verified_model_card
from .routing import activation_boundary_utc, eligible_for_activation


def _require_first_demo_activation(store, card, previous, activation_day, actor, reason):
    if previous is not None or store.db.execute(
        "SELECT 1 FROM active_pointer_history WHERE asset=? LIMIT 1", (card.asset,),
    ).fetchone():
        raise ValueError("demo-now is only allowed for the first activation")
    if (card.asset, card.system_id, card.runtime_key, card.system_version, card.model_version,
            card.timeframe, card.environment.get("environment")) != (
                DEMO_ASSET, DEMO_SYSTEM, DEMO_PROVIDER, DEMO_VERSION, DEMO_VERSION, "M1", "TEST",
            ):
        raise ValueError("demo-now is restricted to the qualified BTCUSDT TEST DMI")
    if (not isinstance(activation_day, datetime) or activation_day.utcoffset() is None
            or activation_day > datetime.now(UTC) or not actor or not reason):
        raise ValueError("demo-now requires an explicit operator and current UTC instant")
    receipt = store.promotion_receipt(hashlib.sha256(card.model_id.encode()).hexdigest())
    body = json.loads(receipt["body"]) if receipt else {}
    if (not receipt or receipt["status"] != "committed"
            or receipt["bundle_hash"] != card.environment.get("forge_manifest_sha256")
            or receipt["research_run_id"] not in card.evidence_refs
            or body.get("environment") != "TEST" or body.get("purpose") != "execution_test"
            or body.get("model_ids") != [card.model_id] or not body.get("authorized_by")):
        raise ValueError("demo-now requires the matching committed qualification receipt")


def publish_active(
    root: Path, registry: ModelArtifactRegistry, card: ModelCard,
    activation_day: str | datetime | date,
    *, store: Store | None = None, provider_resolver=None, actor: str | None = None,
    reason: str | None = None, demo_now: bool = False,
) -> dict[str, Any]:
    if store is None:
        raise ValueError("activation requires Store authority")
    previous = store.active_pointer(card.asset)
    if demo_now:
        _require_first_demo_activation(store, card, previous, activation_day, actor, reason)
    elif not eligible_for_activation(card.created_at_utc, activation_day):
        raise ValueError("activation must be after model creation day")
    boundary = activation_boundary_utc(activation_day)
    created = datetime.fromisoformat(card.created_at_utc)
    cutoff = datetime.fromisoformat(card.fit_cutoff_utc)
    activation = datetime.fromisoformat(card.activation_date_utc) if card.activation_date_utc else boundary
    if any(instant.utcoffset() is None for instant in (boundary, created, cutoff, activation)):
        raise ValueError("model timestamps must be timezone-aware")
    if activation <= created:
        raise ValueError("activation timestamp precedes model creation")
    if activation <= cutoff or boundary <= cutoff:
        raise ValueError("activation timestamp precedes fit cutoff")
    if boundary < activation:
        raise ValueError("activation precedes the card activation boundary")

    metadata = store.get_model_metadata(card.model_id, card.model_version)
    if metadata is None or card_hash(card) != metadata["card_hash"]:
        raise ValueError("model card differs from registered identity")
    verified_model_card(registry, card.model_id, asset=card.asset, metadata=metadata)
    digest = hashlib.sha256(dumps({"card": metadata["card_hash"], "activation_day": boundary.isoformat()}).encode()).hexdigest()
    pointer = store.activate_model(
        card.asset, card.model_id, card.model_version, commit_hash=digest,
        card_hash=metadata["card_hash"], weights_hash=metadata["weights_hash"],
        provider=metadata["provider"], predecessor_model_id=previous["model_id"] if previous else None,
        predecessor_model_version=previous["model_version"] if previous else None,
        registry=registry, provider_resolver=provider_resolver,
    )
    store.event(
        "model_activated", asset=card.asset, model_id=card.model_id, model_version=card.model_version,
        actor=actor or "system", reason=(reason or "explicit_activation"), boundary=boundary.isoformat(),
        activation_policy="explicit_demo_now" if demo_now else "utc_frontier",
    )
    path = Path(root) / "active.yml"
    try:
        write_active_snapshot(store, path)
    except OSError as exc:
        store.register_recovery("active_snapshot_write_failed", {
            "path": str(path), "asset": card.asset, "model_id": card.model_id, "error": str(exc),
        })
        return {**pointer, "snapshot_written": False}
    return {**pointer, "snapshot_written": True}


def load_active(
    root: Path, registry: ModelArtifactRegistry, asset: str, *, store: Store | None = None,
) -> ModelCard | None:
    return ActiveModelResolver(root, registry, store=store).resolve(asset)



def activate_model_explicit(
    root: Path,
    registry: ModelArtifactRegistry,
    *,
    asset: str,
    model_id: str,
    model_version: str,
    store: Store,
    provider_resolver=None,
    actor: str,
    reason: str,
    now: datetime | None = None,
    demo_now: bool = False,
) -> dict[str, Any]:
    """Activate one exact candidate at the current UTC frontier.

    This is intentionally distinct from research promotion: promotion creates
    an eligible registry entry, while this command alone may move the active
    pointer.  No future boundary can be supplied by the caller.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("activation actor is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("activation reason is required")
    card = registry.get(model_id, model_version)
    if card.asset != asset or card.model_version != model_version:
        raise ValueError("activation identity differs")
    current = now or datetime.now().astimezone()
    if current.utcoffset() is None:
        raise ValueError("activation clock must be timezone-aware")
    return publish_active(
        root,
        registry,
        card,
        current,
        store=store,
        provider_resolver=provider_resolver,
        actor=actor.strip(),
        reason=reason.strip(),
        demo_now=demo_now,
    )
