"""Canonical catalog migration and compatibility guards.

The migration manifest is intentionally data-only.  This module provides
read-only verification for legacy aliases and rejects legacy identifiers when
creating new runtime artifacts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

LEGACY_FIELDS = frozenset({"marketplace", "methods", "recipe_id", "method_id", "Arsenal"})
CANONICAL_REQUIRED = frozenset({"system_id", "system_version"})


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file without mutating it."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_mapping_manifest(path: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """Validate migration mappings and return a compact verification report.

    Every source and target must exist and match its recorded digest.  The
    operation is read-only and is safe to run during startup or CI.
    """
    manifest_path = Path(path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("invalid migration manifest")
    mappings = raw.get("mappings")
    if not isinstance(mappings, list):
        raise ValueError("migration mappings must be a list")
    base = Path(root) if root is not None else manifest_path.parent.parent.parent
    checked = 0
    for item in mappings:
        if not isinstance(item, Mapping):
            raise ValueError("migration mapping must be an object")
        for key in ("source", "target", "source_sha256", "target_sha256"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"migration mapping missing {key}")
        source, target = base / item["source"], base / item["target"]
        if not source.is_file() or not target.is_file():
            raise ValueError(f"migration mapping path missing: {item['source']} -> {item['target']}")
        if sha256_file(source) != item["source_sha256"]:
            raise ValueError(f"source hash mismatch: {item['source']}")
        if sha256_file(target) != item["target_sha256"]:
            raise ValueError(f"target hash mismatch: {item['target']}")
        checked += 1
    return {"valid": True, "checked": checked, "manifest": str(manifest_path)}


def reject_legacy_artifact(payload: Mapping[str, Any], *, kind: str = "artifact") -> None:
    """Reject new artifact payloads using pre-cutover names.

    Legacy fields are accepted only by explicit migration readers.  New model,
    card, signal, benchmark and registry writes must identify canonical system
    and model identities.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"{kind} must be a mapping")
    def fields(value):
        if isinstance(value, Mapping):
            found = LEGACY_FIELDS.intersection(value)
            for child in value.values():
                found = found.union(fields(child))
            return found
        if isinstance(value, (list, tuple)):
            return frozenset().union(*(fields(child) for child in value))
        return frozenset()

    present = sorted(fields(payload))
    if present:
        raise ValueError(f"{kind} uses legacy fields: {', '.join(present)}")
    missing = sorted(CANONICAL_REQUIRED - payload.keys())
    if missing and kind in {"model", "model_card", "promotion", "registry"}:
        raise ValueError(f"{kind} requires canonical fields: {', '.join(missing)}")


def read_only_alias(path: str | Path, target: str | Path) -> Path:
    """Resolve a compatibility alias and verify it points at the target.

    No writes are performed.  Callers can use this while reading old paths;
    attempting to use the result as an artifact destination remains prohibited.
    """
    alias, destination = Path(path), Path(target)
    if not alias.exists() or not destination.exists():
        raise ValueError("legacy alias target is missing")
    if alias.is_symlink():
        if alias.resolve() != destination.resolve():
            raise ValueError("legacy alias points to an unexpected target")
    elif alias.read_bytes() != destination.read_bytes():
        raise ValueError("legacy alias bytes differ from canonical target")
    return destination


def verify_canonical_references(payload: Mapping[str, Any], *, kind: str = "artifact") -> dict[str, Any]:
    """Read-only gate for canonical/legacy identity references."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{kind} must be a mapping")
    legacy = sorted(field for field in LEGACY_FIELDS if field in payload)
    canonical = sorted(field for field in CANONICAL_REQUIRED if field in payload)
    if legacy and canonical:
        raise ValueError(f"{kind} mixes canonical and legacy fields: {', '.join(legacy)}")
    if kind in {"model", "model_card", "promotion", "registry"} and len(canonical) != len(CANONICAL_REQUIRED):
        raise ValueError(f"{kind} requires canonical fields: {', '.join(sorted(CANONICAL_REQUIRED - set(canonical)))}")
    return {"kind": kind, "canonical": bool(canonical), "legacy": legacy, "writable": not bool(legacy)}


def verify_replacement_gates(gates: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate replacement gates and execute their read-only checks."""
    from .replacement_gate import executable_evidence, validate_replacement_gate

    results = []
    for gate in gates:
        try:
            validated = validate_replacement_gate(gate)
            evidence = executable_evidence(gate)
            results.append({**validated, "evidence": evidence, "ready_for_removal": evidence["passed"]})
        except (ImportError, ValueError, OSError) as exc:
            results.append({"ready_for_removal": False, "error": str(exc)})
    return {
        "checked": len(results),
        "ready": bool(results) and all(item.get("ready_for_removal") is True for item in results),
        "gates": results,
        "writes": 0,
    }


def read_legacy_active_manifest_payload(root: Path, asset: str) -> dict | None:
    """Inspect a historical manifest without making it an operational pointer."""
    if not isinstance(asset, str) or not asset.isupper() or not asset.isascii() or not asset.isalnum():
        raise ValueError("invalid asset")
    path = Path(root) / f"{asset}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("invalid legacy active manifest") from exc
    if (not isinstance(manifest, dict) or manifest.get("asset") != asset
            or not isinstance(manifest.get("model_id"), str) or not manifest["model_id"]):
        raise ValueError("invalid legacy active manifest identity")
    return manifest


def read_legacy_active_manifest(root: Path, registry, asset: str):
    """Read and verify a historical pointer for migration/audit only."""
    from .models.cards import card_hash

    manifest = read_legacy_active_manifest_payload(root, asset)
    if manifest is None:
        raise ValueError("legacy active manifest is missing")
    cards = [candidate for candidate in registry.list_cards(asset)
             if candidate.model_id == manifest["model_id"]
             and card_hash(candidate) == manifest.get("card_sha256")]
    if len(cards) != 1:
        raise ValueError("legacy active card hash differs or identity is ambiguous")
    card, = cards
    if card.asset != asset or registry.status(card.model_id, card.model_version) != "active":
        raise ValueError("legacy manifest points to a non-active model for the asset")
    if manifest.get("card_sha256") != card_hash(card):
        raise ValueError("legacy active card hash differs")
    if manifest.get("weights_sha256") != card.weights_sha256:
        raise ValueError("legacy active weights hash differs")
    registry.load_weights(card, asset)
    return card


def read_historical_model_card(path: Path):
    """Decode historical metadata without granting it canonical write authority."""
    from .models.cards import _decode_card

    return _decode_card(path)
