"""Writer for the conversational Forge proposal; never runs an experiment."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from .contracts import DEFAULT_WORKSPACE_ROOT, Invalid
from .guard import workspace_layout_guard
from .layout import project_directory, projects_directory
from .validation import (
    artifact_paths,
    knowledge_paths,
    load_workspace,
    read_limited,
    validate_documents,
)


def authoring_context(root, references):
    """Read workspace guidance and only the notes explicitly requested by the author."""
    workspace = load_workspace(root)
    projects_directory(workspace.root)
    notes = {name: path.read_text(encoding="utf-8")
             for name, path in knowledge_paths(workspace, references).items()}
    return {"context": workspace.context, "knowledge": notes}


def publish_proposal(root, proposal_yaml, protocol_yaml):
    """Validate both files, then publish them together without overwriting another proposal."""
    documents = tuple(value.encode("utf-8") if isinstance(value, str) else value
                      for value in (proposal_yaml, protocol_yaml))
    workspace = load_workspace(root)
    result = validate_documents(*documents, workspace=workspace)
    with workspace_layout_guard(workspace.root):
        return _publish(workspace, result.definition.id, documents)


def _publish(workspace, proposal_id, documents):
    target = project_directory(workspace.root, proposal_id)
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f".forge-propose-{target.name}.lock"
    try:
        lock.touch(exist_ok=False)
    except FileExistsError as exc:
        raise Invalid("proposal", "outra proposta está publicando esse ID") from exc

    try:
        if target.exists() or target.is_symlink():
            raise Invalid("proposal", f"{target.name} já existe; proposta não sobrescreve propostas")
        with TemporaryDirectory(prefix=".forge-proposal-", dir=root) as staging:
            draft = Path(staging) / target.name
            draft.mkdir()
            for kind, data in zip(("proposal", "protocol"), documents, strict=True):
                (draft / artifact_paths()[kind]).write_bytes(data)
            if target.exists() or target.is_symlink():
                raise Invalid("proposal", f"{target.name} já existe; proposta não sobrescreve propostas")
            draft.rename(target)
    finally:
        lock.unlink()
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publicador interno da proposta do Tesse Forge")
    parser.add_argument("proposal", type=Path)
    parser.add_argument("protocol", type=Path)
    parser.add_argument("--root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    args = parser.parse_args(argv)
    try:
        print(publish_proposal(args.root, read_limited(args.proposal), read_limited(args.protocol)))
    except (Invalid, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
