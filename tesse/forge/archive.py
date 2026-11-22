"""Explicit, integrity-checked archival through the same lease used by run."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .contracts import Invalid
from .evidence import sha256_file
from .guard import proposal_guard
from .review import open_verified_run
from .validation import artifact_paths, read_limited, versioned_yaml


@dataclass(frozen=True)
class ArchivedProposal:
    path: Path
    proposal_id: str
    run_ids: tuple[str, ...]


def _tree_identity(root):
    def visit(directory):
        for child in directory.iterdir():
            relative = child.relative_to(root)
            if child.is_symlink():
                raise Invalid("archive", f"symlink não permitido no pacote: {relative}")
            if child.is_dir():
                yield relative, None
                yield from visit(child)
            elif child.is_file():
                yield relative, sha256_file(child)
            else:
                raise Invalid("archive", f"artefato não regular: {relative}")

    return dict(visit(root))


def _check_definitions(source, proposal_id):
    paths = artifact_paths()
    for kind, key in (("proposal", "id"), ("protocol", "proposal")):
        filename = paths[kind]
        path = source / filename
        if path.is_symlink() or not path.is_file():
            raise Invalid(filename, "definição ausente ou não regular")
        document = versioned_yaml(read_limited(path), filename)
        if document.get(key) != proposal_id:
            raise Invalid(f"{filename}.{key}", "ID diferente da proposta solicitada")


def _verified_runs(source, proposal_id):
    runs = source / "runs"
    if runs.is_symlink() or (runs.exists() and not runs.is_dir()):
        raise Invalid("runs", "esperado diretório de runs sem symlink")
    if not runs.exists():
        return ()
    ids = []
    for folder in sorted(runs.iterdir()):
        with open_verified_run(folder, expected_proposal=proposal_id, expected_run=folder.name) as (receipt, _):
            ids.append(receipt["run_id"])
    return tuple(ids)


def _check_destination(path):
    if path.parent.is_symlink() or (path.parent.exists() and not path.parent.is_dir()):
        raise Invalid("archive", "esperado diretório de arquivo sem symlink")
    if path.exists() or path.is_symlink():
        raise Invalid("archive", f"destino já existe: {path.name}")


def archive_proposal(root, proposal_id, *, confirmed=False, archive_date=None):
    """Move the verified package as-is after an explicit request; never assesses it."""
    if confirmed is not True:
        raise Invalid("archive", "arquivamento exige pedido explícito")
    if archive_date is None:
        archive_date = date.today()
    if type(archive_date) is not date:
        raise Invalid("archive.date", "esperada data de calendário")
    with proposal_guard(root, proposal_id) as lease:
        source = lease.active_directory()
        destination = lease.workspace.root / "archive" / f"{archive_date.isoformat()}-{proposal_id}"
        _check_destination(destination)
        _check_definitions(source, proposal_id)
        run_ids = _verified_runs(source, proposal_id)
        before = _tree_identity(source)
        destination.parent.mkdir(exist_ok=True)
        _check_destination(destination)
        if lease.active_directory() != source:
            raise Invalid("archive", "a proposta ativa mudou durante a verificação")
        # The destination name is unique to this proposal and date. Other Forge
        # archival calls for this proposal cannot acquire the lease mid-check.
        source.rename(destination)
        if _tree_identity(destination) != before:
            raise Invalid("archive", f"o pacote foi movido para {destination}, mas a verificação posterior divergiu")
        return ArchivedProposal(destination, proposal_id, run_ids)
