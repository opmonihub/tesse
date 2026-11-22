"""Canonical, hash-stable provenance for external model execution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

_VERSIONED = re.compile(r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} is required")
    return value


def _versioned(value: Any, label: str) -> str:
    value = _required_text(value, label)
    if not _VERSIONED.fullmatch(value):
        raise ValueError(f"{label} must be versioned")
    return value


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    library_id: str
    adapter_key: str
    adapter_version: str
    resolved_package: str
    runtime_key: str
    parameters: Mapping[str, Any] | None = None
    hashes: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        _required_text(self.library_id, "library_id")
        _versioned(self.adapter_key, "adapter_key")
        _versioned(self.runtime_key, "runtime_key")
        _required_text(self.adapter_version, "adapter_version")
        _required_text(self.resolved_package, "resolved_package")
        params, hashes = dict(self.parameters or {}), dict(self.hashes or {})
        for key, value in hashes.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not _DIGEST.fullmatch(value)
            ):
                raise ValueError(f"hashes.{key} must be a SHA-256 hex digest")
        object.__setattr__(self, "parameters", params)
        object.__setattr__(self, "hashes", hashes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id,
            "adapter_key": self.adapter_key,
            "adapter_version": self.adapter_version,
            "resolved_package": self.resolved_package,
            "runtime_key": self.runtime_key,
            "parameters": dict(self.parameters or {}),
            "hashes": dict(self.hashes or {}),
        }


def provenance_from_mapping(raw: Mapping[str, Any]) -> ModelProvenance:
    if not isinstance(raw, Mapping):
        raise ValueError("model provenance must be a mapping")
    adapter_key = raw.get("adapter_key")
    adapter_version = raw.get("adapter_version") or (
        adapter_key.rsplit("@", 1)[1]
        if isinstance(adapter_key, str) and "@" in adapter_key
        else None
    )
    library_id = raw.get("library_id") or raw.get("library") or raw.get("inventory_key")
    runtime_key = raw.get("runtime_key") or raw.get("provider")
    resolved = raw.get("resolved_package") or raw.get("resolved_version")
    return ModelProvenance(
        library_id,
        adapter_key,
        adapter_version,
        resolved,
        runtime_key,
        raw.get("parameters"),
        raw.get("hashes"),
    )


def provenance_hash(value: ModelProvenance | Mapping[str, Any]) -> str:
    body = (
        value.to_dict()
        if isinstance(value, ModelProvenance)
        else provenance_from_mapping(value).to_dict()
    )
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def require_adopted_library(provenance: ModelProvenance) -> None:
    """Gate operational provenance from the inventory without importing its adapter."""
    from .library_catalog import library_catalog_path, load_library_catalog

    catalog = load_library_catalog(library_catalog_path())
    library = catalog.get(provenance.library_id)
    if library is None:
        raise ValueError(f"unknown provenance library {provenance.library_id}")
    if library.status != "adopt_now":
        raise ValueError(f"provenance library {library.id} has status {library.status}")
