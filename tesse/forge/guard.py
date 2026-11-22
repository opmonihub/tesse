"""Workspace-local coordination for run, archive and future iteration scopes."""

import os
from contextlib import contextmanager
from errno import EACCES, EAGAIN
from pathlib import Path
from stat import S_ISREG

from ..filelocks import lock_bytes
from .contracts import Invalid, Workspace, slug
from .layout import project_directory, projects_directory
from .validation import load_workspace


class _ProposalLease:
    def __init__(self, workspace, proposal_id):
        self.workspace = workspace
        self.proposal_id = proposal_id
        self._held = True

    def active_directory(self):
        if not self._held:
            raise Invalid("proposal", "a trava da proposta não está mais ativa")
        target = project_directory(self.workspace.root, self.proposal_id)
        if not target.is_dir():
            raise Invalid("proposal", f"proposta ativa não encontrada: {self.proposal_id}")
        return target


def _lock(fd):
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def workspace_layout_guard(root, *, exclusive=False):
    """Shared operation/exclusive migration lease on stable workspace custody."""
    root = Path(root).resolve()
    if os.name == "nt":
        # Windows file handles deny deletion/rename while open; config is
        # required for migration. Lock a byte outside its contents, without IO.
        config = root / "config.yaml"
        if not exclusive and not config.exists():
            yield
            return
        fd = os.open(config, os.O_RDONLY | os.O_BINARY)
    else:
        import fcntl

        # Locking the directory inode leaves no lock artifact, even on failure.
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            if os.name == "nt":
                lock_bytes(fd, 1 << 30, exclusive=exclusive)
            else:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {EACCES, EAGAIN}:
                raise
            raise Invalid("projects.lock", "workspace ocupado por outra operação Forge") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def proposal_guard(root, proposal_id):
    """Hold a nonblocking lease; revalidate the active target after acquisition.

    Keep the same lease throughout execution and publication (or archive checks
    and move); never reacquire it in an inner operation. The lock file is stable
    outside the movable proposal. Closing the descriptor releases the OS lock,
    even on process exit; unlinking the lock file would create competing inodes.
    """
    slug(proposal_id, "proposal")
    workspace = root if isinstance(root, Workspace) else load_workspace(root)
    with workspace_layout_guard(workspace.root):
        projects_directory(workspace.root)
        with _proposal_lease(workspace, proposal_id) as lease:
            yield lease


@contextmanager
def _proposal_lease(workspace, proposal_id):
    directory = workspace.root / ".forge-locks"
    if directory.is_symlink():
        raise Invalid("proposal.lock", "diretório de travas não pode ser um symlink")
    directory.mkdir(exist_ok=True)
    path = directory / f"{proposal_id}.lock"
    if path.is_symlink():
        raise Invalid("proposal.lock", "arquivo de trava não pode ser um symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            raise Invalid("proposal.lock", "esperado arquivo regular para a trava")
        try:
            _lock(fd)
        except OSError as exc:
            if exc.errno not in {EACCES, EAGAIN}:
                raise
            raise Invalid("proposal.lock", f"proposta ocupada: {proposal_id}") from exc
        lease = _ProposalLease(workspace, proposal_id)
        lease.active_directory()
        try:
            yield lease
        finally:
            lease._held = False
    finally:
        os.close(fd)
