"""Canonical active project and historical evidence paths for a selected workspace."""

from pathlib import Path
from re import fullmatch

from .contracts import Invalid, slug

PROJECTS = "projects"
LEGACY_PROJECTS = "proposals"


def require_directory(path, *, missing_ok=False):
    if path.is_symlink() or (not path.is_dir() and (not missing_ok or path.exists())):
        raise Invalid(path.name, "esperado diretório regular sem symlink")
    return path


def projects_directory(root):
    root = Path(root).resolve()
    legacy, projects = root / LEGACY_PROJECTS, root / PROJECTS
    if legacy.is_symlink() or legacy.exists():
        raise Invalid(PROJECTS, "layout legado detectado; use tesse forge migrate-projects "
                      "com o mesmo --root (a migração recusa diretórios em conflito)")
    return require_directory(projects, missing_ok=True)


def project_directory(root, proposal_id):
    slug(proposal_id, "proposal")
    return require_directory(projects_directory(root) / proposal_id, missing_ok=True)


def project_run_directory(root, proposal_id, run_id):
    validate_run_id(run_id)
    project = project_directory(root, proposal_id)
    if not project.is_dir():
        raise Invalid("proposal", f"proposta ativa não encontrada: {proposal_id}")
    return require_directory(project / "runs") / run_id


def validate_run_id(value):
    if not isinstance(value, str) or not fullmatch(r"\d{8}T\d{6}Z-[a-f0-9]{32}", value):
        raise Invalid("run.yaml.run_id", "ID inválido ou diferente da execução solicitada")


def evidence_directories(root):
    root = Path(root).resolve()
    return projects_directory(root), require_directory(root / "archive", missing_ok=True)
