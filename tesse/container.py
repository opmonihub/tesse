"""Preflight checks for the foreground worker used by Docker Compose.

The container is deliberately a supervisor boundary, not a second runtime
authority.  This module only validates that the existing installation is
mounted and usable; it never creates a replacement database, SDK key, or
configuration directory.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import data_path, load_config


class ContainerPreflightError(ValueError):
    """A persistent mount or installation invariant is not safe to operate."""


@dataclass(frozen=True)
class Installation:
    config_path: Path
    data_root: Path
    state_path: Path
    private_path: Path
    sdk_vault: Path
    sdk_key: Path
    lock_path: Path
    account_binding: str | None
    pending_id: str | None


def _lock_is_available(path: Path) -> bool:
    """Probe an existing lock without creating a missing mount or lock file."""
    if not path.exists():
        return True
    if not path.is_file():
        return False
    try:
        with path.open("a+b") as handle:
            if os.name == "nt":  # pragma: no cover - Docker deployment is Linux.
                import msvcrt

                handle.seek(0)
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def _read_existing_state(state_path: Path) -> tuple[str | None, str | None]:
    """Read the persisted account binding without opening a writable Store."""
    try:
        db = sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ContainerPreflightError(f"Banco operacional inválido: {state_path}") from exc
    try:
        row = db.execute("SELECT value FROM meta WHERE key='account_binding'").fetchone()
        pending = db.execute(
            "SELECT id FROM intents WHERE state IN ('DISPATCHING','ACCEPTED','UNKNOWN')"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ContainerPreflightError("Banco operacional inválido ou sem o esquema necessário") from exc
    finally:
        db.close()
    pending_id = pending[0] if pending else None
    if not row:
        return None, pending_id
    # Store values are JSON strings; tolerate a malformed value only by
    # refusing to operate rather than silently treating it as a new account.
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise ContainerPreflightError("Vínculo da conta no banco é inválido") from exc
    if not isinstance(value, str) or not value:
        raise ContainerPreflightError("Vínculo da conta no banco é inválido")
    return value, pending_id


def preflight_installation(config_path: str | Path, *, check_lock: bool = True) -> Installation:
    """Validate an existing installation before the foreground worker opens it.

    The checks intentionally require the mounts and original SDK key to exist;
    no directory, SQLite file, or replacement key is created as a side effect.
    """
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ContainerPreflightError(f"Configuração ausente: {path}")
    if not os.access(path.parent, os.W_OK):
        raise ContainerPreflightError(f"Diretório da configuração não é gravável: {path.parent}")
    try:
        config = load_config(path)
    except Exception as exc:
        raise ContainerPreflightError(f"Configuração inválida: {exc}") from exc
    root = data_path(path, config)
    if not root.is_dir():
        raise ContainerPreflightError(f"Monte a raiz de dados existente: {root}")
    if not os.access(root, os.W_OK):
        raise ContainerPreflightError(f"Raiz de dados não é gravável: {root}")

    state_path = root / "state.sqlite"
    private = root / "private"
    vault = private / "sdk"
    key = private / "sdk.key"
    lock = root / "worker.lock"
    if not state_path.is_file():
        raise ContainerPreflightError(f"Banco operacional ausente: {state_path}")
    if not private.is_dir() or not vault.is_dir():
        raise ContainerPreflightError(f"Cofre/journal do SDK ausente: {vault}")
    if not key.is_file():
        raise ContainerPreflightError(f"Chave original do SDK ausente: {key}")
    if key.stat().st_size != 32:
        raise ContainerPreflightError("Chave inválida: restaure a chave original sem apagar o journal")
    if check_lock and not _lock_is_available(lock):
        raise ContainerPreflightError(f"Outra instância já possui a trava: {lock}")
    binding, pending_id = _read_existing_state(state_path)
    return Installation(path, root, state_path, private, vault, key, lock, binding, pending_id)


def clear_stop_request(config_path: str | Path) -> None:
    """Clear a native/manual stop marker only during an explicit operator action."""
    from .state import Store
    from .worker import ProcessLock

    installation = preflight_installation(config_path)
    with ProcessLock(installation.lock_path), Store(installation.state_path) as store:
        if store.get("stop_requested"):
            store.set("stop_requested", False)
            store.event("worker_resume_requested", source="explicit_operator_action")
