"""Explicit, no-clobber conversion of the workspace's v2 project container."""

import ctypes
import os
import sys
from pathlib import Path

from .contracts import Invalid
from .guard import workspace_layout_guard
from .layout import LEGACY_PROJECTS, PROJECTS, require_directory
from .validation import load_workspace


def _move_noreplace(source, destination):
    """Use one exclusive rename; never implement a move by copying/merging."""
    if os.name == "nt":
        source.rename(destination)  # Windows rename already fails if the target exists.
        return
    library = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(source), os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, old, -100, new, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(old, new, 4)  # RENAME_EXCL
    else:
        raise Invalid(PROJECTS, "plataforma sem rename atômico protegido contra colisão")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(source))


def _check_tree(directory):
    for child in directory.iterdir():
        if child.is_symlink():
            raise Invalid(PROJECTS, f"symlink não permitido na árvore legada: {child}")
        if child.is_dir():
            _check_tree(child)
        elif not child.is_file():
            raise Invalid(PROJECTS, f"artefato não regular na árvore legada: {child}")


def migrate_projects(root):
    """Return whether the selected v2 workspace moved, or was already migrated."""
    selected = Path(root)
    require_directory(selected)
    with workspace_layout_guard(selected, exclusive=True):
        workspace = load_workspace(selected)
        legacy = require_directory(workspace.root / LEGACY_PROJECTS, missing_ok=True)
        projects = require_directory(workspace.root / PROJECTS, missing_ok=True)
        if projects.exists():
            if legacy.exists():
                raise Invalid(PROJECTS, "conflito: proposals/ e projects/ já existem; nenhuma mesclagem foi feita")
            return False
        if not legacy.exists():
            raise Invalid(LEGACY_PROJECTS, "diretório legado ausente; nada para migrar")
        _check_tree(legacy)
        try:
            _move_noreplace(legacy, projects)
        except FileExistsError as exc:
            raise Invalid(PROJECTS, "conflito: destino já existe; nenhuma mesclagem foi feita") from exc
        return True
