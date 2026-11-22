"""Resolve a Store pointer; historical manifests can only be audited."""

from __future__ import annotations

from pathlib import Path

from ..demo_dmi import RUNTIME_KEY as DEMO_DMI_RUNTIME_KEY
from ..demo_dmi import validate_demo_provenance
from ..migration import read_legacy_active_manifest_payload
from ..model_provenance import require_adopted_library
from ..models.cards import ModelCard, card_hash
from ..registry.models import ModelArtifactRegistry
from ..state import Store


def verified_model_card(
    registry: ModelArtifactRegistry, model_id: str, *, asset: str, metadata: dict,
) -> ModelCard:
    card = registry.get(model_id, metadata["model_version"])
    if not card.is_canonical or any((
        card.asset != asset, metadata["asset"] != asset, card.model_id != model_id,
        card.system_id != metadata["system_id"], card.system_version != metadata["system_version"],
        card.model_version != metadata["model_version"], card.timeframe != metadata["timeframe"],
    )):
        raise ValueError("registered model identity differs from card")
    if card_hash(card) != metadata["card_hash"]:
        raise ValueError("registered card hash differs")
    if card.weights_sha256 != metadata["weights_hash"]:
        raise ValueError("registered weights hash differs")
    if not metadata.get("provider") or metadata["provider"] != card.runtime_key:
        raise ValueError("registered provider identity differs")
    provenance = card.provenance
    if card.runtime_key == DEMO_DMI_RUNTIME_KEY:
        validate_demo_provenance(provenance)
    else:
        require_adopted_library(provenance)
    if metadata.get("provenance") != provenance.to_dict():
        raise ValueError("registered provenance differs")
    registry.load_weights(card, asset)
    return card


class ActiveModelResolver:
    def __init__(self, active_root: Path, registry: ModelArtifactRegistry, store: Store | None = None):
        if store is None:
            raise ValueError("model resolution requires Store authority")
        self.active_root = Path(active_root)
        self.registry = registry
        self.store = store

    def _audit_legacy_manifest(self, asset: str, pointer: dict | None) -> None:
        try:
            manifest = read_legacy_active_manifest_payload(self.active_root, asset)
        except ValueError:
            return
        if manifest is None:
            return
        pointer = pointer or {}
        if all(manifest.get(old) == pointer.get(current) for old, current in (
            ("model_id", "model_id"), ("card_sha256", "card_hash"), ("weights_sha256", "weights_hash"),
        )):
            return
        mismatch = {
            "asset": asset, "legacy_model_id": manifest["model_id"],
            "legacy_card_sha256": manifest.get("card_sha256"),
            "active_model_id": pointer.get("model_id"), "active_commit": pointer.get("commit_hash"),
        }
        with self.store.transaction():
            key = f"legacy_manifest_divergence:{asset}"
            if self.store.get(key) != mismatch:
                self.store.event("legacy_manifest_diverged", **mismatch)
                self.store.set(key, mismatch)

    def resolve(self, asset: str) -> ModelCard | None:
        pointer = self.store.active_pointer(asset)
        self._audit_legacy_manifest(asset, pointer)
        if pointer is None:
            return None
        metadata = self.store.get_model_metadata(pointer["model_id"], pointer["model_version"])
        if metadata is None or metadata["status"] != "active":
            raise ValueError("active pointer has no active metadata")
        if pointer["model_version"] != metadata["model_version"]:
            raise ValueError("active pointer version differs from model metadata")
        return verified_model_card(self.registry, pointer["model_id"], asset=asset, metadata=metadata)

    def version(self, asset: str) -> str | None:
        card = self.resolve(asset)
        return card.model_id if card else None
