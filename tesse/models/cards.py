"""Immutable model cards and serialization helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import atomic_write
from ..model_provenance import provenance_from_mapping, provenance_hash

STATUSES = {"challenger", "eligible", "active", "retired", "rejected"}
# Runtime providers are executable identities, not recipe or inventory names.
_VERSIONED_RUNTIME_KEY = re.compile(r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$")


def validate_runtime_key(value: str) -> str:
    if not isinstance(value, str) or not _VERSIONED_RUNTIME_KEY.fullmatch(value):
        raise ValueError("runtime_key must be a versioned provider key (name@major.minor.patch)")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _validate_digest(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest") from exc


@dataclass(frozen=True, slots=True)
class ModelCard:
    """Immutable model metadata.

    ``recipe_id`` and the original hash fields remain readable for migrated
    cards. New canonical cards should provide system/model identities and
    provenance refs; legacy recipe-only cards are accepted for compatibility
    while migration aliases remain read-only.
    """
    asset: str
    recipe_id: str | None = None
    model_id: str = ""
    status: str = "challenger"
    created_at_utc: str = ""
    activation_date_utc: str | None = None
    fit_cutoff_utc: str = ""
    snapshot_sha256: str = ""
    recipe_sha256: str = ""
    weights_path: str = ""
    weights_sha256: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    parent_model_id: str | None = None
    system_id: str | None = None
    system_version: str | None = None
    model_version: str | None = None
    timeframe: str | None = None
    provider: str | None = None
    library_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    card_sha256: str | None = None
    # Canonical provenance keeps inventory, capability and executable runtime
    # identities separate. These fields are optional only for historical cards.
    library_id: str | None = None
    adapter_key: str | None = None
    adapter_version: str | None = None
    resolved_package: str | None = None
    runtime_key: str | None = None

    def __post_init__(self):
        from ..registry.identity import (
            TIMEFRAMES,
            validate_model_id,
            validate_system_id,
            validate_version,
        )
        if not isinstance(self.asset, str) or not self.asset.isupper() or not self.asset.isalnum():
            raise ValueError("asset must be an uppercase symbol")
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("model_id is required")
        if any(token in self.model_id for token in ("/", "\\", "..")):
            raise ValueError("model_id must be a path-safe identifier")
        if not isinstance(self.status, str) or self.status not in STATUSES:
            raise ValueError("unknown model status")
        if not isinstance(self.weights_path, str):
            raise ValueError("weights_path must be relative to the model store")
        if (Path(self.weights_path).is_absolute() or ".." in Path(self.weights_path).parts or "\\" in self.weights_path):
            raise ValueError("weights_path must be relative to the model store")
        for name in ("snapshot_sha256", "recipe_sha256", "weights_sha256"):
            _validate_digest(getattr(self, name), name)
        if self.system_id is not None:
            if self.recipe_id is not None:
                raise ValueError("canonical cards cannot use legacy recipe_id")
            validate_system_id(self.system_id)
            if self.system_version is None or self.model_version is None or self.timeframe is None:
                raise ValueError("canonical cards require system/model versions and timeframe")
            validate_version(self.system_version)
            validate_version(self.model_version)
            if self.timeframe not in TIMEFRAMES:
                raise ValueError("unknown timeframe")
            validate_model_id(self.model_id, system_id=self.system_id, asset=self.asset)
            if self.runtime_key is None:
                # During migration a runtime key may be carried in the environment
                # projection, but canonical cards must still expose it explicitly.
                candidate = (self.environment or {}).get("runtime_key")
                if candidate:
                    object.__setattr__(self, "runtime_key", candidate)
            if self.runtime_key is None:
                raise ValueError("canonical cards require runtime_key")
            validate_runtime_key(self.runtime_key)
            if self.adapter_key is None or self.library_id is None or self.resolved_package is None:
                raise ValueError("canonical cards require library_id, adapter_key and resolved_package")
            try:
                provenance_from_mapping({"library_id": self.library_id, "adapter_key": self.adapter_key,
                    "adapter_version": self.adapter_version, "resolved_package": self.resolved_package,
                    "runtime_key": self.runtime_key})
            except ValueError as exc:
                raise ValueError(f"invalid canonical provenance: {exc}") from exc
        elif self.recipe_id is None:
            raise ValueError("canonical card requires system_id; legacy card requires recipe_id")
        if not isinstance(self.library_refs, tuple):
            object.__setattr__(self, "library_refs", tuple(self.library_refs or ()))
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs or ()))

    @property
    def provenance(self):
        if not self.is_canonical:
            return None
        return provenance_from_mapping({"library_id": self.library_id, "adapter_key": self.adapter_key,
            "adapter_version": self.adapter_version, "resolved_package": self.resolved_package,
            "runtime_key": self.runtime_key, "parameters": (self.environment or {}).get("parameters", {}),
            "hashes": (self.environment or {}).get("provenance_hashes", {})})

    @property
    def provenance_sha256(self) -> str | None:
        value = self.provenance
        return provenance_hash(value) if value is not None else None

    @property
    def is_canonical(self) -> bool:
        return self.system_id is not None

    def identity_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("status", None)
        value.pop("card_sha256", None)
        return value


def card_hash(card: ModelCard) -> str:
    return hashlib.sha256(_canonical(card.identity_payload())).hexdigest()


def save_card(path: Path, card: ModelCard) -> None:
    if not card.is_canonical:
        raise ValueError("historical cards cannot be written as canonical artifacts")
    from ..migration import reject_legacy_artifact

    payload = asdict(card)
    payload.pop("recipe_id")
    reject_legacy_artifact(payload, kind="model_card")
    atomic_write(Path(path), yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def load_card(path: Path) -> ModelCard:
    card = _decode_card(path)
    if not card.is_canonical:
        raise ValueError("legacy card requires the historical migration reader")
    return card


def _decode_card(path: Path) -> ModelCard:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid model card: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("model card must be a mapping")
    try:
        return ModelCard(**raw)
    except TypeError as exc:
        raise ValueError("model card fields are invalid") from exc
