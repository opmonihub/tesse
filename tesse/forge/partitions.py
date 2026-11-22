"""Coordinate pinned development exposure with final window reservations."""

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from tempfile import NamedTemporaryFile

from ..filelocks import lock_bytes
from .contracts import Invalid
from .layout import require_directory

FINAL_LOCK_NAME = ".forge-final.lock"
DEVELOPMENT_EXPOSURES = ".forge-development-exposures"


@contextmanager
def partition_lock(root):
    """Use one workspace lock for both sides of the partition boundary."""
    path = Path(root).resolve() / FINAL_LOCK_NAME
    if path.is_symlink():
        raise Invalid("partition.lock", "trava não pode ser symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            raise Invalid("partition.lock", "esperado arquivo regular")
        if os.name == "nt":
            try:
                lock_bytes(fd, 0, exclusive=True)
            except BlockingIOError as exc:
                raise Invalid("partition.lock", "partições ocupadas por outra operação Forge") from exc
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def overlap(a, b):
    return a.start < b.end and b.start < a.end


def verify_managed_span(manifest, lineage, windows, field):
    if manifest.lineage != lineage:
        raise Invalid("snapshot.lineage", "linhagem do protocolo não corresponde ao manifesto verificado")
    for name, window in windows.items():
        if window.start < manifest.span.start or window.end > manifest.span.end:
            raise Invalid(f"{field}.{name}", "snapshot verificado não cobre a janela")


def write_durable_json(path, document):
    """Commit file contents before the rename, and the containing directory after it."""
    payload = json.dumps(document, sort_keys=True, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    tmp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".draft-", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            for directory in (path.parent, path.parent.parent):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def development_exposures(root, lineage):
    from .iteration import _window

    directory = require_directory(Path(root) / DEVELOPMENT_EXPOSURES, missing_ok=True)
    for path in directory.glob("*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("arquivo não regular")
            data = json.loads(path.read_bytes())
            if type(data) is not dict or not isinstance(data.get("lineage"), str):
                raise ValueError("linhagem de exposição inválida")
            window = _window(data.get("window"), "exposure.window")
        except (OSError, ValueError) as exc:
            raise Invalid("development.exposure", f"exposição não verificável: {path.name}") from exc
        if data["lineage"] == lineage:
            yield window


def record_development_exposure(lease, experiment, manifest):
    from .iteration import _known_exposures

    root = lease.workspace.root
    lease.active_directory()
    lineage = experiment.managed_lineage.key
    document = {"lineage": lineage, "window": manifest.span.as_dict(), "snapshot": dict(experiment.snapshot)}
    key = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    with partition_lock(root):
        for window, _ in _known_exposures(root, lineage, strict=True):
            if overlap(window, manifest.span):
                raise Invalid("snapshot.span", "snapshot de desenvolvimento cruza intervalo final reservado ou exposto")
        directory = require_directory(root / DEVELOPMENT_EXPOSURES, missing_ok=True)
        directory.mkdir(exist_ok=True)
        target = directory / f"{key}.json"
        if target.is_symlink():
            raise Invalid("development.exposure", "exposição não pode ser symlink")
        if target.exists():
            if not target.is_file():
                raise Invalid("development.exposure", "exposição não é um arquivo regular")
            current = json.loads(target.read_bytes())
            if not isinstance(current, dict) or any(current.get(k) != v for k, v in document.items()):
                raise Invalid("development.exposure", "exposição existente não corresponde ao snapshot")
        else:
            write_durable_json(target, document | {
                "proposal": lease.proposal_id, "experiment": experiment.id,
                "registered_at": datetime.now(UTC).isoformat(),
            })
