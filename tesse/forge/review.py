"""Read-only integrity checks and separately approved publication of reviewed notes."""

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .contracts import Invalid, mapping, slug, text
from .evidence import EVIDENCE_NAME, TABLE_FIELDS, TABLES, sha256_file
from .guard import workspace_layout_guard
from .layout import project_run_directory, validate_run_id
from .study_evidence import verify_extension
from .validation import artifact_paths, load_workspace, versioned_yaml


@contextmanager
def open_verified_run(folder, *, expected_proposal=None, expected_run=None):
    """Open a self-contained run without using workspace context, snapshots, or Store."""
    folder = Path(folder).absolute()
    if folder.is_symlink() or not folder.is_dir():
        raise Invalid("run", "esperado diretório de uma execução publicada sem symlink")
    manifest = folder / Path(artifact_paths()["run"]).name
    database = folder / EVIDENCE_NAME
    for artifact in (manifest, database):
        if artifact.is_symlink() or not artifact.is_file():
            raise Invalid(artifact.name, "artefato publicado ausente ou não regular")
    if set(folder.iterdir()) != {manifest, database}:
        raise Invalid("run", "pasta da execução contém artefatos adicionais ou arquivos auxiliares")
    receipt = versioned_yaml(manifest.read_bytes(), manifest.name, limit=None)
    proposal = slug(receipt.get("proposal"), "run.yaml.proposal")
    slug(receipt.get("experiment"), "run.yaml.experiment")
    run_id = text(receipt.get("run_id"), "run.yaml.run_id")
    validate_run_id(run_id)
    if expected_run is not None and expected_run != run_id:
        raise Invalid("run.yaml.run_id", "ID inválido ou diferente da execução solicitada")
    if expected_proposal is not None and expected_proposal != proposal:
        raise Invalid("run.yaml.proposal", "ID diferente da proposta solicitada")
    if receipt.get("status") != "completed":
        raise Invalid("run.yaml.status", "recibo de execução não concluída")
    kind = text(receipt.get("result_kind"), "run.yaml.result_kind")
    if kind not in TABLES:
        raise Invalid("run.yaml.result_kind", "tipo de resultado desconhecido")
    verdicts = ("passed", "failed") if kind == "comparison" else ("not_evaluated",)
    if receipt.get("verdict") not in verdicts:
        raise Invalid("run.yaml.verdict", "veredito incompatível com o tipo de resultado")
    mapping(receipt.get("metrics"), "run.yaml.metrics")
    hashes = mapping(receipt.get("artifact_hashes"), "run.yaml.artifact_hashes")
    if set(hashes) != {EVIDENCE_NAME} or sha256_file(database) != hashes[EVIDENCE_NAME]:
        raise Invalid(EVIDENCE_NAME, "SHA-256 não confere com o recibo da execução")
    table = TABLES[kind]
    try:
        # Finalized evidence has no WAL/journal. Immutable, read-only access must
        # not create sidecars or perform recovery in the copy being inspected.
        connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise Invalid(EVIDENCE_NAME, f"evidência SQLite ilegível: {exc}") from exc
    with closing(connection):
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            tables = connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
            columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
            if columns != TABLE_FIELDS[table] or ("study" not in receipt and tables != [(table,)]):
                raise Invalid(EVIDENCE_NAME, "tabela de evidência incompatível com o recibo")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise Invalid(EVIDENCE_NAME, "falha na checagem de integridade do SQLite")
            if "study" in receipt:
                verify_extension(connection, receipt, [name for name, in tables])
        except sqlite3.Error as exc:
            raise Invalid(EVIDENCE_NAME, f"evidência SQLite inválida: {exc}") from exc
        yield receipt, connection


def review_run(folder, *, expected_proposal=None, expected_run=None):
    """Report integrity and scope; absence of independent evidence proves nothing."""
    with open_verified_run(folder, expected_proposal=expected_proposal, expected_run=expected_run) as (receipt, db):
        kind = receipt["result_kind"]
        table = TABLES[kind]
        rows = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if "study" in receipt:
            scope = "development"
        elif kind == "comparison" and receipt["metrics"].get("improvement_pp") is None:
            scope = "inconclusive"
        elif receipt.get("phase") == "development":
            scope = "development"
        elif kind == "predictions":
            scope = "descriptive"
        else:
            scope = "independence_not_established"
        return {**{key: receipt[key] for key in ("proposal", "run_id", "experiment", "status", "verdict", "metrics")},
                "scope": scope, "evidence_sha256": receipt["artifact_hashes"][EVIDENCE_NAME],
                "table": table, "rows": rows}


def review_project_run(root, proposal_id, run_id):
    """Review an exact active project/run in the selected workspace; no fallback."""
    with workspace_layout_guard(root):
        return review_run(project_run_directory(root, proposal_id, run_id),
                          expected_proposal=proposal_id, expected_run=run_id)


@dataclass(frozen=True)
class KnowledgeNote:
    title: str
    claim: str
    evidence: str
    conditions: str
    decision: str

    def markdown(self):
        sections = (("Afirmação verificável", self.claim), ("Evidência auditável sem replay", self.evidence),
                    ("Condições de uso e revalidação", self.conditions), ("Decisão reutilizável", self.decision))
        title = text(self.title, "knowledge.title").strip()
        body = "\n\n".join(f"## {name}\n\n{text(value, name).strip()}" for name, value in sections)
        return f"# {title}\n\n{body}\n"


def publish_knowledge_note(root, concept_id, note, *, approved=False):
    """Write an already-audited draft only after approval of that exact note."""
    if approved is not True:
        raise Invalid("knowledge", "publicação exige aprovação humana explícita da nota")
    slug(concept_id, "knowledge.id")
    body = note.markdown()
    directory = load_workspace(root).root / "knowledge"
    if directory.is_symlink():
        raise Invalid("knowledge", "diretório de notas não pode ser um symlink")
    directory.mkdir(exist_ok=True)
    path = directory / f"{concept_id}.md"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(body)
    return path
