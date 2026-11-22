"""Transactional upgrade of the operational registry's identity key."""

import sqlite3
from pathlib import Path

from ..models.cards import card_hash
from .models import ModelArtifactRegistry

_TABLES = {
    "model_metadata": """(
        model_id TEXT NOT NULL, system_id TEXT NOT NULL, system_version TEXT NOT NULL,
        model_version TEXT NOT NULL, asset TEXT NOT NULL, timeframe TEXT NOT NULL,
        status TEXT NOT NULL, card_hash TEXT NOT NULL, weights_hash TEXT NOT NULL,
        provider TEXT, library_refs TEXT NOT NULL, evidence_refs TEXT NOT NULL,
        provenance TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(model_id, model_version))""",
    "lifecycle_transitions": """(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL, model_version TEXT NOT NULL,
        from_status TEXT, to_status TEXT NOT NULL, reason TEXT, at TEXT NOT NULL,
        FOREIGN KEY(model_id, model_version) REFERENCES model_metadata(model_id, model_version))""",
    "active_pointers": """(
        asset TEXT PRIMARY KEY, model_id TEXT NOT NULL, model_version TEXT NOT NULL,
        commit_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(model_id, model_version) REFERENCES model_metadata(model_id, model_version))""",
    "active_pointer_history": """(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL, model_id TEXT NOT NULL,
        model_version TEXT NOT NULL, commit_hash TEXT NOT NULL, predecessor_model_id TEXT,
        operation_id TEXT, changed_at TEXT NOT NULL, predecessor_model_version TEXT,
        CHECK((predecessor_model_id IS NULL) = (predecessor_model_version IS NULL)),
        FOREIGN KEY(model_id, model_version) REFERENCES model_metadata(model_id, model_version),
        FOREIGN KEY(predecessor_model_id, predecessor_model_version)
            REFERENCES model_metadata(model_id, model_version))""",
    "legacy_model_bindings": """(
        model_id TEXT PRIMARY KEY NOT NULL, model_version TEXT NOT NULL,
        card_path TEXT NOT NULL UNIQUE, weights_path TEXT NOT NULL,
        FOREIGN KEY(model_id, model_version) REFERENCES model_metadata(model_id, model_version))""",
}


def ensure_versioned_model_schema(store, root: Path):
    db = store.db
    with store.transaction():
        primary = tuple(row["name"] for row in db.execute("PRAGMA table_info(model_metadata)") if row["pk"])
        if primary == ("model_id", "model_version"):
            return
        if not primary:
            for name, columns in _TABLES.items():
                db.execute(f"CREATE TABLE {name}{columns}")
            return
        if primary != ("model_id",):
            raise ValueError("model identity migration: unrecognized registry schema")
        try:
            _upgrade(db, ModelArtifactRegistry(root))
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise ValueError(f"model identity migration: {exc}") from exc


def _upgrade(db, artifacts):
    legacy_tables = tuple(name for name in _TABLES if name != "legacy_model_bindings")
    for name in legacy_tables:
        db.execute(f"ALTER TABLE {name} RENAME TO migration_{name}")
    for name, columns in _TABLES.items():
        db.execute(f"CREATE TABLE {name}{columns}")
    db.execute("INSERT INTO model_metadata SELECT * FROM migration_model_metadata")
    for metadata in db.execute("SELECT * FROM model_metadata"):
        identity = metadata["model_id"], metadata["model_version"]
        path, card = artifacts._find(*identity)
        if (
            any(getattr(card, field) != metadata[field] for field in ("asset", "system_id", "system_version", "timeframe"))
            or card_hash(card) != metadata["card_hash"]
            or card.weights_sha256 != metadata["weights_hash"]
        ):
            raise ValueError(f"legacy artifact does not match registered identity: {identity}")
        artifacts.load_weights(card, metadata["asset"])
        db.execute("INSERT INTO legacy_model_bindings VALUES(?,?,?,?)", (
            *identity, path.relative_to(artifacts.root).as_posix(), card.weights_path,
        ))
    db.execute("""INSERT INTO lifecycle_transitions
        SELECT t.seq,t.model_id,m.model_version,t.from_status,t.to_status,t.reason,t.at
        FROM migration_lifecycle_transitions t LEFT JOIN model_metadata m USING(model_id)""")
    db.execute("INSERT INTO active_pointers SELECT * FROM migration_active_pointers")
    db.execute("""INSERT INTO active_pointer_history
        SELECT h.*,m.model_version FROM migration_active_pointer_history h
        LEFT JOIN model_metadata m ON h.predecessor_model_id=m.model_id""")
    for name in reversed(legacy_tables):
        db.execute(f"DROP TABLE migration_{name}")
