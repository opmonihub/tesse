"""Finalize and atomically publish portable evidence for completed experiments."""

import hashlib
import os
import platform
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from errno import EEXIST, ENOTEMPTY
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import numpy as np
import yaml

from .. import __version__
from .components import _stamp
from .contracts import Invalid
from .layout import validate_run_id
from .study_evidence import EXTENSION, canonical_digest, write_extension
from .validation import SCHEMA_VERSION, artifact_paths

EVIDENCE_NAME = "evidence.sqlite"
TABLES = {"comparison": "trades", "predictions": "predictions"}
TRADE_FIELDS = ("arm", "asset", "anchor_at", "decision_at", "entry_at", "expiry_at", "label_at", "direction", "outcome")
PREDICTION_FIELDS = ("asset", "anchor_at", "direction", "probability_win")
TABLE_FIELDS = {"trades": TRADE_FIELDS, "predictions": PREDICTION_FIELDS}

CREATE_TRADES = """
CREATE TABLE trades (
    arm TEXT NOT NULL CHECK (arm IN ('control', 'candidate')),
    asset TEXT NOT NULL,
    anchor_at TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    entry_at TEXT NOT NULL,
    expiry_at TEXT NOT NULL,
    label_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('CALL', 'PUT')),
    outcome TEXT NOT NULL CHECK (outcome IN ('WIN', 'LOSS', 'TIE')),
    PRIMARY KEY (arm, asset, anchor_at)
) STRICT
"""

CREATE_PREDICTIONS = """
CREATE TABLE predictions (
    asset TEXT NOT NULL,
    anchor_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('CALL', 'PUT')),
    probability_win REAL NOT NULL CHECK (probability_win BETWEEN 0.0 AND 1.0),
    PRIMARY KEY (asset, anchor_at)
) STRICT
"""

def _engine_hash():
    package = files("tesse")
    paths = [f"forge/{f.name}" for f in package.joinpath("forge").iterdir()
             if f.name.endswith((".py", ".yaml"))]
    paths += ["adapters/contracts.py", "adapters/registry.py", "adapters/sklearn_adapter.py",
              "data.py", "library_catalog.py", "primitives/__init__.py",
              "primitives/features.py", "primitives/ranking.py"]
    digest = hashlib.sha256()
    for name in sorted(paths):
        digest.update(name.encode() + b"\0" + hashlib.sha256(package.joinpath(*name.split("/")).read_bytes()).digest())
    return digest.hexdigest()


def sha256_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _evidence_rows(kind, asset, output):
    if kind == "comparison":
        for arm in ("control", "candidate"):
            for trade in getattr(output, arm).trades:
                yield (arm, *(trade[field] for field in TRADE_FIELDS[1:]))
    else:
        for anchor, side, probability in zip(output.frame.anchors, output.frame.sides, output.probabilities, strict=True):
            yield asset, _stamp(anchor), "CALL" if side == 1 else "PUT", float(probability)


def _write_evidence(path, kind, asset, outputs, extension=None):
    table = TABLES[kind]
    marks = ",".join("?" for _ in TABLE_FIELDS[table])
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                connection.execute("BEGIN")
                connection.execute(CREATE_TRADES if kind == "comparison" else CREATE_PREDICTIONS)
                for output in outputs:
                    connection.executemany(f"INSERT INTO {table} VALUES ({marks})", _evidence_rows(kind, asset, output))
                count = write_extension(connection, *extension) if extension else None
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise Invalid(path.name, "falha na checagem de integridade do SQLite")
    except sqlite3.Error as exc:
        raise Invalid(path.name, f"não foi possível concluir evidência SQLite: {exc}") from exc
    if set(path.parent.iterdir()) != {path}:
        raise Invalid(path.name, "arquivos auxiliares permaneceram após fechar a evidência")
    return sha256_file(path), count


def _assessment(kind, output):
    if kind == "predictions":
        return {"prediction_count": len(output.probabilities)}, {}, "not_evaluated"
    metrics = {key: value for key, value in output.metrics.items()
               if key not in {"criteria_pass", "meets_study_criteria"}}
    gates = output.metrics["criteria_pass"]
    return metrics, gates, "passed" if all(gates.values()) else "failed"


def _check_runs(directory):
    if directory.is_symlink():
        raise Invalid("runs", "diretório de runs não pode ser um symlink")
    if directory.exists() and not directory.is_dir():
        raise Invalid("runs", "esperado um diretório de runs")


def _publish(staged_runs, draft, directory, destination):
    _check_runs(directory)
    if not directory.exists():
        try:
            staged_runs.rename(directory)
            return
        except OSError as exc:
            if exc.errno not in (EEXIST, ENOTEMPTY):
                raise
            _check_runs(directory)  # Another first run may have just been published.
    if destination.exists() or destination.is_symlink():
        raise Invalid("run_id", "ID de execução já existe")
    draft.rename(destination)


class _ReceiptDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        # The read contract forbids YAML aliases even when metadata legitimately
        # shares a Python list (e.g. the binding and its portable projection).
        return True


def new_run_id():
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex}"


def publish_run(lease, validation, experiment, execution, metadata=None):
    return _publish_execution(lease, validation, experiment, execution, metadata=metadata)


def publish_study_run(lease, validation, experiment, execution, *, run_id=None, metadata=None):
    if experiment.managed_lineage is None or execution.study.experiment != experiment.id:
        raise Invalid("study", "evidência gerenciada exige experimento correspondente")
    if [block.id for block in execution.study.blocks] != list(execution.blocks):
        raise Invalid("study.blocks", "a publicação exige a bateria inteira em ordem")
    binding = {"phase": "development", **(metadata or {})}
    return _publish_execution(lease, validation, experiment, next(iter(execution.blocks.values())),
                              metadata=binding, study_run=execution, run_id=run_id)


def _sync_file(path):
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class PrePublicationFailure(Invalid):
    """The staging operation raised before any rename to the public run path."""


@contextmanager
def _staging_errors(managed):
    try:
        yield
    except (OSError, ValueError) as exc:
        if not managed:
            raise
        if isinstance(exc, Invalid):
            raise PrePublicationFailure(exc.field, exc.message) from exc
        raise PrePublicationFailure("study.evidence", str(exc)) from exc


def _publish_execution(lease, validation, experiment, execution, *, metadata=None, study_run=None, run_id=None):
    proposal = validation.definition
    if (proposal is None or proposal.id != lease.proposal_id or validation.workspace != lease.workspace
            or proposal.experiments.get(experiment.id) is not experiment):
        raise Invalid("experiment", "a publicação exige a proposta e o experimento validados sob a trava")
    proposal_path = lease.active_directory()
    directory = proposal_path / "runs"
    _check_runs(directory)
    created = datetime.now(UTC)
    run_id = run_id or new_run_id()
    validate_run_id(run_id)
    destination = directory / run_id
    manifest_name = Path(artifact_paths()["run"]).name
    kind, snapshot = experiment.result_kind, execution.snapshot
    snapshot_identity = {**experiment.snapshot, "asset": snapshot.asset, "timeframe": "M1",
                         "source": snapshot.source, "span": snapshot.span.as_dict(), "rows": len(snapshot.bars)}
    if study_run is None:
        outputs = (execution.output,)
        metrics, gates, verdict = _assessment(kind, execution.output)
        training = execution.training
        extension = None
    else:
        blocks = study_run.blocks
        if any(result.snapshot is not snapshot for result in blocks.values()):
            raise Invalid("study.blocks", "os blocos não compartilham o snapshot verificado")
        outputs = tuple(result.output for result in blocks.values())
        records = {block_id: _assessment(kind, result.output) for block_id, result in blocks.items()}
        metrics = {"blocks": {block_id: record[0] for block_id, record in records.items()}}
        gates = {block_id: record[1] for block_id, record in records.items()}
        training = {"blocks": {block_id: result.training for block_id, result in blocks.items()}}
        verdict = study_run.verdict
        expected = canonical_digest(experiment.as_dict())
        if metadata.get("effective_hash", expected) != expected:
            raise Invalid("study", "vínculo invalida o experimento efetivo")
        metadata = metadata | {"snapshot_identity": snapshot_identity, "effective_hash": expected}
        extension = (study_run.study, records, blocks, metadata)
    with TemporaryDirectory(prefix=".forge-run-", dir=proposal_path) as staging:
        with _staging_errors(study_run is not None):
            staged_runs = Path(staging) / "runs"
            draft = staged_runs / run_id
            draft.mkdir(parents=True)
            evidence_hash, count = _write_evidence(draft / EVIDENCE_NAME, kind, snapshot.asset, outputs,
                                                    extension)
            manifest = {
                "schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "completed",
                "created_at": created.isoformat(), "proposal": proposal.id, "experiment": experiment.id,
                "result_kind": kind, "verdict": verdict,
                "definition_hashes": dict(validation.hashes), "effective_proposal": proposal.as_dict(),
                "effective_experiment": experiment.as_dict(),
                "effective_settings": {"data_root": str(execution.data_root)},
                "snapshot": snapshot_identity,
                "versions": {"tesse": __version__, "engine_sha256": _engine_hash(),
                             "python": platform.python_version(), "numpy": np.__version__,
                             "pyyaml": yaml.__version__, "sqlite": sqlite3.sqlite_version},
                "training": training, "metrics": metrics,
                "checks": {"snapshot_verified": True, "criteria": gates},
                "artifact_hashes": {EVIDENCE_NAME: evidence_hash},
            }
            if study_run is not None:
                manifest["study"] = {
                    "schema": EXTENSION, "round_id": study_run.study.round_id,
                    "declaration_hash": study_run.study.digest, "declaration": study_run.study.as_dict(),
                    "opportunity_count": count, "binding": dict(metadata),
                }
            if metadata:
                # Iteration metadata is additive and remains in the portable receipt;
                # legacy standalone runs pass no metadata.
                for key, value in metadata.items():
                    if key in manifest:
                        raise Invalid("run.metadata", f"campo reservado: {key}")
                    manifest[key] = value
            (draft / manifest_name).write_text(yaml.dump(manifest, Dumper=_ReceiptDumper, sort_keys=False, allow_unicode=True),
                                              encoding="utf-8")
            if study_run is not None:
                from .review import open_verified_run

                with open_verified_run(draft, expected_proposal=proposal.id, expected_run=run_id):
                    pass
                _sync_file(draft / manifest_name)
                _sync_file(draft / EVIDENCE_NAME)
                _sync_directory(draft)
        _publish(staged_runs, draft, directory, destination)
        if study_run is not None:
            _sync_directory(directory)
            _sync_directory(proposal_path)
    return {"proposal": proposal.id, "experiment": experiment.id, "run_id": run_id,
            "manifest": str(destination / manifest_name), "result_kind": kind,
            "status": "completed", "verdict": verdict, "metrics": metrics}
