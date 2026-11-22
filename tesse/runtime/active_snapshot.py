"""Auditable active-pointer projection backed by the SQLite Store."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ..config import atomic_write
from ..state import Store


def canonical_active_snapshot_payload(pointers: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the stable payload used for snapshot integrity.

    Mutable timestamps are intentionally excluded; pointer identity and the
    declared activation boundary are the only inputs to the digest.
    """
    return {
        "active": {
            row["asset"]: {
                "model_id": row["model_id"],
                "model_version": row["model_version"],
                "commit_hash": row["commit_hash"],
            }
            for row in sorted(pointers, key=lambda item: item["asset"])
        }
    }


def active_snapshot_hash(pointers_or_payload: list[dict[str, Any]] | dict[str, Any]) -> str:
    payload = (canonical_active_snapshot_payload(pointers_or_payload)
               if isinstance(pointers_or_payload, list) else pointers_or_payload)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()


def _commit_hash(pointers: list[dict[str, Any]]) -> str:
    return active_snapshot_hash(pointers)


def write_active_snapshot(store: Store, path: str | Path) -> dict[str, Any]:
    pointers = store.active_pointers()
    payload = canonical_active_snapshot_payload(pointers)
    digest = active_snapshot_hash(payload)
    snapshot = {
        "schema_version": 2,
        "snapshot_hash": digest,
        "commit_hash": digest,  # compatibility projection
        **payload,
    }
    path = Path(path)
    atomic_write(path, yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True))
    return snapshot


def _valid_snapshot(snapshot: Any, expected_payload: dict[str, Any], expected_hash: str) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") not in {1, 2}:
        return False
    active = snapshot.get("active")
    if not isinstance(active, dict):
        return False
    normalized = {"active": {}}
    for asset, value in active.items():
        if not isinstance(value, dict) or not value.get("model_id") or not value.get("model_version"):
            return False
        normalized["active"][asset] = {
            "model_id": value["model_id"],
            "model_version": value["model_version"],
            "commit_hash": value.get("commit_hash", ""),
        }
    # Version 1 snapshots had no per-asset commit hash; they are stale by design
    # and are rebuilt once into the v2 canonical form.
    return normalized == expected_payload and snapshot.get("snapshot_hash", snapshot.get("commit_hash")) == expected_hash and snapshot.get("schema_version") == 2


def reconcile_active_snapshot(store: Store, path: str | Path) -> dict[str, Any]:
    """Reconcile a projection; malformed/stale snapshots never win."""
    path = Path(path)
    snapshot = None
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                snapshot = loaded
        except (OSError, UnicodeError, yaml.YAMLError):
            snapshot = None
    pointers = store.active_pointers()
    expected_payload = canonical_active_snapshot_payload(pointers)
    expected = active_snapshot_hash(expected_payload)
    stale = not _valid_snapshot(snapshot, expected_payload, expected)
    result = write_active_snapshot(store, path) if stale else snapshot
    if stale:
        store.event("active_snapshot_reconciled", stale_snapshot=True,
                    previous_commit=(snapshot or {}).get("snapshot_hash", (snapshot or {}).get("commit_hash")),
                    commit_hash=expected)
        store.register_recovery("active_snapshot", {"path": str(path), "expected_hash": expected,
                                                     "previous_hash": (snapshot or {}).get("snapshot_hash")}, resolved=True)
    return {"snapshot": result, "stale": stale, "snapshot_hash": expected}
