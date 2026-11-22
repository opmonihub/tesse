"""Bounded, resumable Forge development iterations and isolated confirmation.

The iteration controller deliberately lives beside the v2 Forge runner.  It owns
only local planning state and journal metadata; experiments still execute through
``execute_experiment`` and publish the same portable ``run.yaml``/SQLite
artifacts as ``tesse forge run``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Protocol
from uuid import uuid4

import yaml

from .contracts import Invalid, Window, Workspace, keys, mapping, number, slug, text
from .guard import proposal_guard, workspace_layout_guard
from .layout import evidence_directories, project_directory, projects_directory, require_directory
from .partitions import (
    development_exposures,
    overlap,
    partition_lock,
    record_development_exposure,
    verify_managed_span,
    write_durable_json,
)
from .study import Study, parse_study_declaration
from .study_state import StudyState
from .validation import (
    artifact_paths,
    parse_utc,
    read_limited,
    validate_documents,
    validate_proposal,
    versioned_yaml,
)

ITERATION_SCHEMA_VERSION = 2
JOURNAL_NAME = "journal.sqlite"
ITERATION_NAME = "iteration.yaml"
STATE_NAME = "state.json"
FINAL_RESERVATIONS = ".forge-final-reservations"


# ---------------------------------------------------------------------------
# Small, strict data contracts


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_yaml(path: Path) -> tuple[dict, bytes]:
    try:
        raw = read_limited(path)
    except OSError as exc:
        raise Invalid(str(path), f"não foi possível ler: {exc}") from exc
    return versioned_yaml(raw, path.name), raw


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _window(value: Any, field: str) -> Window:
    data = keys(value, field, required=("start", "end"))
    start, end = parse_utc(data["start"], f"{field}.start"), parse_utc(data["end"], f"{field}.end")
    if start >= end:
        raise Invalid(field, "start precisa ser anterior a end")
    return Window(start, end)


def _window_map(value: Any, field: str, *, names=("train", "calibration", "evaluation")) -> dict[str, Window]:
    data = keys(value, field, required=names)
    result: dict[str, Window] = {}
    previous = None
    for name in names:
        current = _window(data[name], f"{field}.{name}")
        if previous is not None and current.start < previous.end:
            raise Invalid(f"{field}.{name}", "janelas fora de ordem ou sobrepostas")
        result[name] = current
        previous = current
    return result


def _identity(value: Any, field: str) -> dict[str, Any]:
    data = mapping(value, field)
    if not data:
        raise Invalid(field, "identidade fixada não pode ser vazia")
    # Snapshot identities use the v2 manifest/NPY hashes.  Other pinned IDs are
    # accepted as explicit opaque strings so synthetic reviewers can be tested.
    if "manifest" in data:
        required = {"manifest", "manifest_sha256", "sha256"}
        if set(data) != required:
            raise Invalid(field, "snapshot exige manifest, manifest_sha256 e sha256")
        if not isinstance(data["manifest"], str) or not data["manifest"].endswith(".json"):
            raise Invalid(f"{field}.manifest", "manifesto JSON inválido")
        for key in ("manifest_sha256", "sha256"):
            value = data[key]
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise Invalid(f"{field}.{key}", "esperado SHA-256 hexadecimal minúsculo")
    else:
        for key, value in data.items():
            text(value, f"{field}.{key}")
    return copy.deepcopy(data)


@dataclass(frozen=True)
class AllowedChange:
    path: str
    type: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    values: tuple[Any, ...] = ()

    def validate(self, value: Any) -> Any:
        types = {
            "integer": int,
            "number": (int, float),
            "float": (int, float),
            "boolean": bool,
            "string": str,
        }
        expected = types.get(self.type)
        if expected is None:
            raise Invalid(f"allowed_changes.{self.path}.type", f"tipo não suportado: {self.type}")
        if self.type == "integer" and type(value) is not int:
            raise Invalid(f"adjustment.{self.path}", "esperado inteiro")
        if self.type == "number" and (type(value) not in (int, float) or isinstance(value, bool)):
            raise Invalid(f"adjustment.{self.path}", "esperado número")
        if self.type == "float" and (type(value) not in (int, float) or isinstance(value, bool)):
            raise Invalid(f"adjustment.{self.path}", "esperado número")
        if self.type == "boolean" and type(value) is not bool:
            raise Invalid(f"adjustment.{self.path}", "esperado booleano")
        if self.type == "string" and not isinstance(value, str):
            raise Invalid(f"adjustment.{self.path}", "esperado texto")
        if self.minimum is not None and value < self.minimum:
            raise Invalid(f"adjustment.{self.path}", f"valor abaixo do mínimo {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise Invalid(f"adjustment.{self.path}", f"valor acima do máximo {self.maximum}")
        if self.values and value not in self.values:
            raise Invalid(f"adjustment.{self.path}", "valor não está no conjunto permitido")
        return value


@dataclass(frozen=True)
class ReviewerSpec:
    role: str
    backend: str
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IterationPlan:
    proposal_id: str
    experiment_id: str
    schema_version: int
    control: Mapping[str, Any]
    development_snapshot: Mapping[str, Any]
    development_windows: Mapping[str, Window]
    criteria: Mapping[str, float]
    allowed_changes: tuple[AllowedChange, ...]
    max_rounds: int
    max_reviewer_calls: int
    automatic: bool
    stop: Mapping[str, Any]
    reviewers: tuple[ReviewerSpec, ...]
    confirmation_snapshot: Mapping[str, Any]
    confirmation_window: Window
    confirmation_lineage: str
    label_maturity_seconds: int
    raw: Mapping[str, Any]
    plan_hash: str
    protocol_hash: str | None = None
    initial_study: Study | None = None

    @property
    def reviewer_roles(self) -> tuple[str, ...]:
        return tuple(spec.role for spec in self.reviewers)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.raw))


def _normalize_change_path(path: Any, field: str) -> str:
    path = text(path, field).strip(".")
    prefixes = ("candidate.", "experiment.", "experiments.")
    for prefix in prefixes:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if not path.startswith("nodes.") or path.count(".") < 3:
        raise Invalid(field, "ajuste deve apontar um único parâmetro em nodes.<node>.params.<param>")
    parts = path.split(".")
    if parts[2] != "params" or len(parts) != 4 or any(not p for p in parts):
        raise Invalid(field, "ajuste deve apontar exatamente um parâmetro de nó")
    return path


def _plan_sections(raw: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    # Accept the compact aliases used by early operator drafts while emitting a
    # single canonical contract in iteration.yaml.
    development = raw.get("development", raw.get("data"))
    if development is None:
        raise Invalid("plan.development", "seção de desenvolvimento obrigatória")
    confirmation = raw.get("confirmation", raw.get("final"))
    if confirmation is None:
        raise Invalid("plan.confirmation", "seção de confirmação obrigatória")
    budget = raw.get("budget")
    if budget is None:
        raise Invalid("plan.budget", "orçamento obrigatório")
    allowed = raw.get("allowed_changes", raw.get("candidate_parameters"))
    if allowed is None:
        raise Invalid("plan.allowed_changes", "allowlist de parâmetros obrigatória")
    return mapping(development, "plan.development"), mapping(confirmation, "plan.confirmation"), mapping(budget, "plan.budget"), mapping({"allowed": allowed}, "plan.allowed_changes")


def _control_nodes(experiment) -> set[str]:
    if experiment is None:
        return set()
    parents = {node.id: {source for source, _ in node.inputs.values()} for node in experiment.nodes}
    result = next(node for node in experiment.nodes if node.id == experiment.result[0])
    targets = [result.inputs["control"][0]] if "control" in result.inputs else [result.id]
    seen = set()
    stack = targets
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        stack.extend(parents.get(node_id, ()))
    # A comparison graph has explicit control/candidate branches.  The result
    # node itself and all of its ancestors are immutable for an iteration; the
    # candidate branch is deliberately allowed only where it is not shared.
    return seen


def parse_iteration_plan(raw: Mapping[str, Any], *, proposal_id: str, experiment_id: str,
                         protocol_hash: str | None = None, experiment=None) -> IterationPlan:
    raw = mapping(dict(raw), "plan")
    allowed_top = {"schema_version", "proposal", "experiment", "control", "development", "data",
                   "allowed_changes", "candidate_parameters", "budget", "stop", "stopping", "reviewers",
                   "confirmation", "final", "description", "version", "notes", "initial_candidate", "study"}
    unknown = set(raw) - allowed_top
    if unknown:
        raise Invalid("plan." + sorted(unknown)[0], "campo desconhecido")
    if raw.get("schema_version") != ITERATION_SCHEMA_VERSION:
        raise Invalid("plan.schema_version", f"versão esperada: {ITERATION_SCHEMA_VERSION}")
    declared_proposal = slug(raw.get("proposal"), "plan.proposal")
    declared_experiment = slug(raw.get("experiment"), "plan.experiment")
    if declared_proposal != proposal_id:
        raise Invalid("plan.proposal", "não corresponde à proposta solicitada")
    if declared_experiment != experiment_id:
        raise Invalid("plan.experiment", "não corresponde ao experimento solicitado")
    development, confirmation, budget, allowed_section = _plan_sections(raw)
    development_snapshot = _identity(development.get("snapshot"), "plan.development.snapshot")
    development_windows = _window_map(development.get("windows"), "plan.development.windows")
    criteria = mapping(development.get("criteria"), "plan.development.criteria")
    required_criteria = {"min_winrate", "min_trades_per_day", "min_improvement_pp"}
    if set(criteria) != required_criteria:
        raise Invalid("plan.development.criteria", "critérios precisam ser os três critérios de comparação v2")
    criteria = {key: number(criteria[key], f"plan.development.criteria.{key}", low=0) for key in required_criteria}
    if experiment is not None:
        if getattr(experiment, "result_kind", None) != "comparison":
            raise Invalid("plan.experiment", "iterações exigem experimento de comparação")
        if dict(experiment.criteria or {}) != criteria:
            raise Invalid("plan.development.criteria", "critérios diferem do experimento congelado")
        if dict(experiment.snapshot) != development_snapshot:
            raise Invalid("plan.development.snapshot", "snapshot difere do protocolo congelado")
        if {k: v.as_dict() for k, v in experiment.windows.items()} != {k: v.as_dict() for k, v in development_windows.items()}:
            raise Invalid("plan.development.windows", "janelas diferem do protocolo congelado")
    allowed_raw = allowed_section["allowed"]
    if isinstance(allowed_raw, dict):
        allowed_raw = [{"path": path, **(spec if isinstance(spec, dict) else {"type": spec})}
                       for path, spec in allowed_raw.items()]
    if not isinstance(allowed_raw, list) or not 1 <= len(allowed_raw) <= 64:
        raise Invalid("plan.allowed_changes", "esperada lista de 1 a 64 parâmetros")
    allowed_changes = []
    seen = set()
    for index, item in enumerate(allowed_raw):
        field = f"plan.allowed_changes.{index}"
        item = keys(item, field, required=("path", "type"), optional=("min", "max", "minimum", "maximum", "values"))
        path = _normalize_change_path(item["path"], f"{field}.path")
        if experiment is not None:
            try:
                _lookup_path(experiment.as_dict(), path)
            except Invalid as exc:
                raise Invalid(f"{field}.path", "parâmetro não existe no experimento congelado") from exc
            if path.split(".")[1] in _control_nodes(experiment):
                raise Invalid(f"{field}.path", "controle é imutável; escolha um parâmetro exclusivo do candidato")
        if path in seen:
            raise Invalid(field, "parâmetro repetido")
        seen.add(path)
        minimum = item.get("min", item.get("minimum"))
        maximum = item.get("max", item.get("maximum"))
        if minimum is not None and maximum is not None and minimum > maximum:
            raise Invalid(field, "intervalo inválido")
        values = item.get("values", [])
        if not isinstance(values, list):
            raise Invalid(f"{field}.values", "esperada lista")
        allowed_changes.append(AllowedChange(path, text(item["type"], f"{field}.type"), minimum, maximum, tuple(values)))
    max_rounds = number(budget.get("max_rounds", budget.get("rounds")), "plan.budget.max_rounds", low=0, integer=True)
    max_calls = number(budget.get("max_reviewer_calls", budget.get("reviewer_calls", 0)), "plan.budget.max_reviewer_calls", low=0, integer=True)
    automatic = budget.get("automatic", budget.get("auto", False))
    if type(automatic) is not bool:
        raise Invalid("plan.budget.automatic", "esperado booleano")
    stop = raw.get("stop", raw.get("stopping", {}))
    if not isinstance(stop, dict):
        raise Invalid("plan.stop", "esperado mapeamento")
    reviewer_data = raw.get("reviewers", {})
    if isinstance(reviewer_data, list):
        reviewer_items = [(str(item.get("role", "")), item) for item in reviewer_data if isinstance(item, dict)]
    elif isinstance(reviewer_data, dict):
        reviewer_items = list(reviewer_data.items())
    else:
        raise Invalid("plan.reviewers", "esperado mapa ou lista")
    reviewers = []
    for role, item in reviewer_items:
        role = text(role, "plan.reviewers.role")
        if isinstance(item, str):
            item = {"backend": item}
        item = mapping(item, f"plan.reviewers.{role}")
        backend = text(item.get("backend", "synthetic"), f"plan.reviewers.{role}.backend")
        reviewers.append(ReviewerSpec(role, backend, copy.deepcopy(item)))
    required_roles = {"protocol", "evidence", "calibration"}
    if "study" in raw and len({spec.role for spec in reviewers}) != len(reviewers):
        raise Invalid("plan.reviewers", "cada papel de crítico gerenciado precisa ser único")
    if {spec.role for spec in reviewers} != required_roles:
        missing = sorted(required_roles - {spec.role for spec in reviewers})
        extra = sorted({spec.role for spec in reviewers} - required_roles)
        detail = "ausentes: " + ", ".join(missing) if missing else "papéis não permitidos: " + ", ".join(extra)
        raise Invalid("plan.reviewers", "críticos obrigatórios de protocolo, evidência e calibração (" + detail + ")")
    confirmation_snapshot = _identity(confirmation.get("snapshot"), "plan.confirmation.snapshot")
    if confirmation_snapshot == development_snapshot:
        raise Invalid("plan.confirmation.snapshot", "confirmação exige payload separado do desenvolvimento")
    confirmation_window = _window(confirmation.get("window", confirmation.get("windows", {}).get("evaluation")), "plan.confirmation.window")
    if confirmation_window.start < development_windows["evaluation"].end:
        raise Invalid("plan.confirmation.window", "confirmação precisa ser posterior à avaliação de desenvolvimento")
    maturity = number(confirmation.get("label_maturity_seconds", 182), "plan.confirmation.label_maturity_seconds", low=0, integer=True)
    if development_windows["evaluation"].end + timedelta(seconds=maturity) > confirmation_window.start:
        raise Invalid("plan.confirmation.window", "fronteira não respeita maturidade dos rótulos")
    lineage = text(confirmation.get("lineage", confirmation_snapshot.get("lineage", "")), "plan.confirmation.lineage")
    control = mapping(raw.get("control"), "plan.control")
    if control.get("experiment", experiment_id) != experiment_id:
        raise Invalid("plan.control.experiment", "controle precisa apontar para o experimento selecionado")
    canonical = copy.deepcopy(raw)
    initial_study = None
    if experiment is not None:
        managed = experiment.managed_lineage
        if managed is not None and "study" not in raw:
            raise Invalid("plan.study", "experimento gerenciado exige declaração inicial")
        if managed is None and "study" in raw:
            raise Invalid("plan.study", "declaração exige experimento gerenciado")
        if managed is not None and lineage != managed.key:
            raise Invalid("plan.confirmation.lineage", "precisa corresponder à linhagem gerenciada do protocolo")
    if "study" in raw:
        initial_study = parse_study_declaration(raw["study"], proposal_id, experiment_id, development_windows)
        canonical["study"] = initial_study.as_dict()
    canonical["proposal"] = proposal_id
    canonical["experiment"] = experiment_id
    return IterationPlan(proposal_id, experiment_id, ITERATION_SCHEMA_VERSION, control,
                         development_snapshot, development_windows, criteria, tuple(allowed_changes),
                         max_rounds, max_calls, automatic, copy.deepcopy(stop), tuple(reviewers),
                         confirmation_snapshot, confirmation_window, lineage, maturity, canonical,
                         _hash(canonical), protocol_hash, initial_study)


def load_iteration_plan(path: Path, *, proposal_id: str, experiment_id: str, protocol_hash: str | None = None,
                        experiment=None) -> IterationPlan:
    data, _ = _read_yaml(Path(path))
    return parse_iteration_plan(data, proposal_id=proposal_id, experiment_id=experiment_id,
                                protocol_hash=protocol_hash, experiment=experiment)


# ---------------------------------------------------------------------------
# Journal and iteration state


@dataclass(frozen=True)
class IterationState:
    root: Path
    proposal_id: str
    iteration_id: str
    plan: IterationPlan
    status: str
    round: int
    plan_hash: str
    events: tuple[dict[str, Any], ...]
    checkpoint: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id, "iteration_id": self.iteration_id,
            "status": self.status, "round": self.round, "plan_hash": self.plan_hash,
            "events": list(self.events), "checkpoint": dict(self.checkpoint),
            "path": str(self.root),
        }


def _journal_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            phase TEXT NOT NULL,
            round INTEGER,
            payload TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS checkpoints (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS budget (
            name TEXT PRIMARY KEY,
            reserved INTEGER NOT NULL DEFAULT 0,
            consumed INTEGER NOT NULL DEFAULT 0
        ) STRICT;
        CREATE TABLE IF NOT EXISTS identities (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) STRICT;
        """
    )


_MISSING = object()


class IterationJournal:
    def __init__(self, path: Path, *, readonly=False):
        self.path = Path(path)
        if readonly:
            self.connection = sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            _journal_schema(self.connection)
        self.connection.row_factory = sqlite3.Row

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def event(self, kind: str, *, phase="development", round=None, payload=None) -> int:
        payload = {} if payload is None else payload
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO events(at,kind,phase,round,payload) VALUES(?,?,?,?,?)",
                (_timestamp(), kind, phase, round, json.dumps(payload, sort_keys=True, ensure_ascii=False)),
            )
        return int(cursor.lastrowid)

    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple({**dict(row), "payload": json.loads(row["payload"])} for row in self.connection.execute("SELECT * FROM events ORDER BY seq"))

    def checkpoint(self, key: str, value: Any = _MISSING) -> Any:
        if value is _MISSING:
            row = self.connection.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
            return None if row is None else json.loads(row["value"])
        with self.connection:
            self.connection.execute("INSERT INTO checkpoints(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, sort_keys=True, ensure_ascii=False)))
        return value

    def checkpoints(self) -> dict[str, Any]:
        return {row["key"]: json.loads(row["value"]) for row in self.connection.execute("SELECT key,value FROM checkpoints")}

    def identity(self, key: str, value: Any | None = None) -> Any:
        if value is not None:
            with self.connection:
                self.connection.execute("INSERT INTO identities(name,value) VALUES(?,?) ON CONFLICT(name) DO NOTHING", (key, json.dumps(value, sort_keys=True, ensure_ascii=False)))
        row = self.connection.execute("SELECT value FROM identities WHERE name=?", (key,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def reserve(self, name: str, amount: int = 1, *, limit: int | None = None) -> bool:
        if amount < 1:
            raise Invalid("budget", "reserva precisa ser positiva")
        with self.connection:
            row = self.connection.execute("SELECT reserved,consumed FROM budget WHERE name=?", (name,)).fetchone()
            reserved, consumed = (int(row[0]), int(row[1])) if row else (0, 0)
            if limit is not None and reserved + consumed + amount > limit:
                return False
            self.connection.execute("INSERT INTO budget(name,reserved,consumed) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET reserved=excluded.reserved", (name, reserved + amount, consumed))
        return True

    def consume(self, name: str, amount: int = 1) -> None:
        with self.connection:
            row = self.connection.execute("SELECT reserved,consumed FROM budget WHERE name=?", (name,)).fetchone()
            if row is None or int(row[0]) < amount:
                raise Invalid("budget", f"reserva ausente para {name}")
            self.connection.execute("UPDATE budget SET reserved=reserved-?, consumed=consumed+? WHERE name=?", (amount, amount, name))

    def budget(self) -> dict[str, dict[str, int]]:
        return {row["name"]: {"reserved": row["reserved"], "consumed": row["consumed"]} for row in self.connection.execute("SELECT name,reserved,consumed FROM budget")}


def _state_paths(root: Path, proposal_id: str, iteration_id: str) -> tuple[Path, Path]:
    proposal = project_directory(root, proposal_id)
    target = require_directory(proposal / "iterations", missing_ok=True) / iteration_id
    return target, target / ITERATION_NAME


def _iteration_id() -> str:
    return f"iter-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:12]}"


def _safe_iteration_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("iter-") or len(value) > 80:
        raise Invalid("iteration", "ID de iteração inválido")
    slug(value, "iteration")
    return value


def _complete_pending_creation(lease, iteration_id):
    """Recover the exact prepared directory only if its ownership was committed."""
    _safe_iteration_id(iteration_id)
    target, _ = _state_paths(lease.workspace.root, lease.proposal_id, iteration_id)
    if target.exists() or target.is_symlink():
        return
    pending_root = require_directory(target.parent / ".pending", missing_ok=True)
    pending = require_directory(pending_root / iteration_id, missing_ok=True)
    if not pending.is_dir():
        return
    record, _ = _read_yaml(pending / ITERATION_NAME)
    if (record.get("proposal") != lease.proposal_id or record.get("iteration_id") != iteration_id
            or StudyState(lease, record.get("experiment")).owner != iteration_id):
        raise Invalid("study.owner", "preparação interrompida sem posse verificável")
    from .evidence import _sync_directory

    pending.rename(target)
    _sync_directory(target.parent)


def _ensure_frozen_definition(lease, proposal_id: str, manifest: Mapping[str, Any]) -> None:
    validation = validate_proposal(lease.workspace, proposal_id)
    if not validation.valid():
        issue = validation.issues[0]
        raise Invalid(issue.path, issue.message)
    expected = manifest.get("proposal_hashes", {})
    if expected and dict(validation.hashes) != dict(expected):
        raise Invalid("iteration", "proposta ou protocolo mudou depois do congelamento")


def _load_iteration(root: Path, proposal_id: str, iteration_id: str) -> tuple[Path, dict[str, Any], IterationPlan]:
    _safe_iteration_id(iteration_id)
    directory, path = _state_paths(root, proposal_id, iteration_id)
    if directory.is_symlink() or not directory.is_dir():
        raise Invalid("iteration", "diretório da iteração ausente ou não regular")
    if path.is_symlink() or not path.is_file():
        raise Invalid("iteration", "iteração não encontrada")
    data, _ = _read_yaml(path)
    plan_data = data.get("plan")
    if not isinstance(plan_data, dict):
        raise Invalid("iteration.yaml.plan", "plano congelado ausente")
    journal = directory / JOURNAL_NAME
    if journal.is_symlink() or not journal.is_file():
        raise Invalid("journal.sqlite", "diário da iteração ausente ou não regular")
    plan = parse_iteration_plan(plan_data, proposal_id=proposal_id, experiment_id=plan_data["experiment"], protocol_hash=data.get("protocol_hash"))
    if data.get("iteration_id") != iteration_id or data.get("proposal") != proposal_id or data.get("experiment") != plan.experiment_id:
        raise Invalid("iteration", "identidade do manifesto não corresponde ao caminho solicitado")
    if data.get("plan_hash") != plan.plan_hash:
        raise Invalid("iteration.plan_hash", "hash do plano congelado não confere")
    if data.get("control_hash") and data["control_hash"] != _hash(plan.control):
        raise Invalid("iteration.control_hash", "hash do controle congelado não confere")
    return directory, data, plan


def inspect_iteration(root, proposal_id: str, iteration_id: str) -> dict[str, Any]:
    with workspace_layout_guard(root):
        return _inspect_iteration(root, proposal_id, iteration_id)


def _inspect_iteration(root, proposal_id: str, iteration_id: str) -> dict[str, Any]:
    directory, manifest, plan = _load_iteration(root, proposal_id, iteration_id)
    with IterationJournal(directory / JOURNAL_NAME, readonly=True) as journal:
        if plan.initial_study is not None:
            from .managed_iteration import inspect_managed

            return inspect_managed(root, directory, plan, journal)
        events = journal.events()
        checkpoint = journal.checkpoints()
        budgets = journal.budget()
    status = checkpoint.get("status", manifest.get("status", "paused"))
    current_round = int(checkpoint.get("round", 0))
    return IterationState(directory, proposal_id, iteration_id, plan, status, current_round, manifest.get("plan_hash", plan.plan_hash), events, checkpoint).as_dict() | {"budgets": budgets}


# ---------------------------------------------------------------------------
# Plan creation and adjustment validation


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def create_iteration(root, proposal_id: str, experiment_id: str, plan_path: Path, *, iteration_id: str | None = None, data_root=None) -> dict[str, Any]:
    iteration_id = _safe_iteration_id(iteration_id) if iteration_id else _iteration_id()
    with proposal_guard(root, proposal_id) as lease:
        validation = validate_proposal(lease.workspace, proposal_id)
        if not validation.valid():
            issue = validation.issues[0]
            raise Invalid(issue.path, issue.message)
        experiment = validation.definition.experiments.get(experiment_id)
        if experiment is None:
            raise Invalid("experiment", f"experimento não encontrado: {experiment_id}")
        from .graph import require_supported_route

        require_supported_route(experiment)
        if experiment.result_kind != "comparison":
            raise Invalid("plan.experiment", "iterações exigem experimento de comparação")
        plan = load_iteration_plan(plan_path, proposal_id=proposal_id, experiment_id=experiment_id,
                                   protocol_hash=validation.hashes[artifact_paths()["protocol"]], experiment=experiment)
        state = StudyState(lease, experiment_id)
        if state.exists:
            raise Invalid("study.owner", f"o experimento pertence a {state.owner}")
        if plan.initial_study is not None:
            from .runner import preflight_study

            preflight_study(experiment, plan.initial_study, lease.workspace.effective_data_root(data_root),
                            confirmation_window=plan.confirmation_window)
        target = lease.active_directory() / "iterations" / iteration_id
        if target.parent.is_symlink() or (target.parent.exists() and not target.parent.is_dir()):
            raise Invalid("iterations", "diretório de iterações não pode ser symlink")
        if target.exists() or target.is_symlink():
            raise Invalid("iteration", f"ID já existe: {iteration_id}")
        target.parent.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix=".forge-iteration-", dir=target.parent) as staging_dir:
            staging = Path(staging_dir) / iteration_id
            staging.mkdir()
            frozen = {
                "schema_version": ITERATION_SCHEMA_VERSION, "iteration_id": iteration_id,
                "proposal": proposal_id, "experiment": experiment_id,
                "created_at": _timestamp(), "status": "paused", "round": 0,
                "plan_hash": plan.plan_hash, "proposal_hashes": dict(validation.hashes),
                "protocol_hash": validation.hashes[artifact_paths()["protocol"]],
                "control_hash": _hash(plan.control), "plan": plan.as_dict(),
                "control": copy.deepcopy(dict(plan.control)),
                "development": {"snapshot": copy.deepcopy(dict(plan.development_snapshot)),
                                "windows": {k: v.as_dict() for k, v in plan.development_windows.items()},
                                "criteria": dict(plan.criteria)},
                "allowed_changes": [{"path": item.path, "type": item.type, "min": item.minimum,
                                     "max": item.maximum, "values": list(item.values)} for item in plan.allowed_changes],
                "budget": {"max_rounds": plan.max_rounds, "max_reviewer_calls": plan.max_reviewer_calls,
                           "automatic": plan.automatic},
                "stop": copy.deepcopy(dict(plan.stop)),
                "confirmation": {"snapshot": copy.deepcopy(dict(plan.confirmation_snapshot)),
                                 "window": plan.confirmation_window.as_dict(), "lineage": plan.confirmation_lineage,
                                 "label_maturity_seconds": plan.label_maturity_seconds},
            }
            (staging / ITERATION_NAME).write_text(yaml.safe_dump(frozen, sort_keys=False, allow_unicode=True), encoding="utf-8")
            with IterationJournal(staging / JOURNAL_NAME) as journal:
                journal.identity("iteration_id", iteration_id)
                journal.identity("proposal_id", proposal_id)
                journal.identity("reviewers", list(plan.reviewer_roles))
                journal.identity("reviewer_ids", {spec.role: spec.config.get("reviewer_id", f"{spec.backend}:{spec.role}") for spec in plan.reviewers})
                journal.identity("control_hash", frozen["control_hash"])
                journal.identity("plan_hash", plan.plan_hash)
                journal.checkpoint("status", "paused")
                journal.checkpoint("round", 0)
                if plan.initial_study is not None:
                    journal.checkpoint("managed_data_root", str(lease.workspace.effective_data_root(data_root)))
                journal.event("plan_frozen", payload={"plan_hash": plan.plan_hash, "proposal_hashes": dict(validation.hashes)})
            if plan.initial_study is not None:
                from .evidence import _sync_directory, _sync_file

                pending_root = require_directory(target.parent / ".pending", missing_ok=True)
                pending_root.mkdir(exist_ok=True)
                pending = pending_root / iteration_id
                if pending.exists() or pending.is_symlink():
                    raise Invalid("iteration", f"ID já tem preparação pendente: {iteration_id}")
                _sync_file(staging / ITERATION_NAME)
                _sync_file(staging / JOURNAL_NAME)
                _sync_directory(staging)
                staging.rename(pending)
                _sync_directory(pending_root)
                _sync_directory(target.parent)
                state.claim(iteration_id)
                _complete_pending_creation(lease, iteration_id)
            else:
                staging.rename(target)
    return inspect_iteration(root, proposal_id, iteration_id)


def _lookup_path(document: Any, path: str) -> tuple[Any, str]:
    parts = path.split(".")
    cursor = document
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise Invalid(f"adjustment.{path}", "parâmetro não existe no protocolo")
        cursor = cursor[part]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        raise Invalid(f"adjustment.{path}", "parâmetro não existe no protocolo")
    return cursor, parts[-1]


def validate_adjustment(plan: IterationPlan, adjustment: Mapping[str, Any], *, protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    adjustment = mapping(dict(adjustment), "adjustment")
    if set(adjustment) - {"path", "value", "reason", "source"} or "path" not in adjustment or "value" not in adjustment:
        raise Invalid("adjustment", "esperados path e value")
    path = _normalize_change_path(adjustment["path"], "adjustment.path")
    allowed = next((item for item in plan.allowed_changes if item.path == path), None)
    if allowed is None:
        raise Invalid("adjustment.path", "parâmetro fora da allowlist congelada")
    value = allowed.validate(adjustment["value"])
    if protocol is not None:
        container, key = _lookup_path(protocol, path)
        if container[key] == value:
            raise Invalid("adjustment.value", "ajuste não altera o valor congelado")
    result = {"path": path, "value": value}
    for key in ("reason", "source"):
        if key in adjustment:
            result[key] = text(adjustment[key], f"adjustment.{key}")
    return result


def adjustment_hash(adjustment: Mapping[str, Any] | None) -> str:
    if adjustment is None:
        return _hash({"path": None, "value": None})
    return _hash({"path": adjustment["path"], "value": adjustment["value"]})


def _apply_adjustment(protocol: dict[str, Any], experiment_id: str, adjustment: Mapping[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(protocol)
    if adjustment is None:
        return result
    path = adjustment["path"]
    # Iteration paths are relative to the selected experiment's nodes.
    if path.startswith("nodes."):
        path = "experiments." + experiment_id + "." + path
    container, key = _lookup_path(result, path)
    container[key] = copy.deepcopy(adjustment["value"])
    return result


# ---------------------------------------------------------------------------
# Reviewer isolation and reconciliation


class Reviewer(Protocol):
    def review(self, evidence: Mapping[str, Any]) -> Mapping[str, Any]: ...


class SyntheticReviewer:
    """Deterministic opt-in reviewer used by tests and local dry runs."""

    def __init__(self, role: str, config: Mapping[str, Any] | None = None):
        self.role, self.config = role, dict(config or {})

    def review(self, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        recommendation = self.config.get("recommendation")
        response = {
            "reviewer_id": self.config.get("reviewer_id", f"synthetic:{self.role}"),
            "role": self.role,
            "decision": "adjust" if recommendation else "hold",
            "evidence_hash": _hash(evidence),
            "findings": list(self.config.get("findings", [])),
            "limit": self.config.get("limit", "development-only"),
        }
        if recommendation is not None:
            response["adjustment"] = copy.deepcopy(recommendation)
        return response


class JsonProcessReviewer:
    """Explicit JSON-I/O reviewer adapter without shell, tools or credentials."""

    def __init__(self, role: str, command, *, timeout: float = 30):
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ReviewerUnavailable(f"comando JSON inválido para crítico {role}")
        self.role, self.command, self.timeout = role, tuple(command), timeout

    def review(self, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            result = subprocess.run(self.command, input=json.dumps(dict(evidence), ensure_ascii=False),
                                    capture_output=True, text=True, timeout=self.timeout,
                                    check=False, shell=False, env={"LC_ALL": "C", "LANG": "C"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewerUnavailable(f"crítico {self.role} indisponível: {exc}") from exc
        if result.returncode != 0:
            raise ReviewerUnavailable(f"crítico {self.role} retornou {result.returncode}")
        try:
            response = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise Invalid("reviewer.response", "resposta JSON inválida") from exc
        return response


class ReviewerUnavailable(RuntimeError):
    pass


def _reviewer_from_spec(spec: ReviewerSpec, reviewers: Mapping[str, Reviewer] | None) -> Reviewer:
    if reviewers and spec.role in reviewers:
        return reviewers[spec.role]
    if spec.backend == "synthetic":
        return SyntheticReviewer(spec.role, spec.config)
    if spec.backend in {"process", "json"}:
        return JsonProcessReviewer(spec.role, spec.config.get("command"), timeout=float(spec.config.get("timeout", 30)))
    raise ReviewerUnavailable(f"backend de crítico não habilitado: {spec.backend}")


def validate_recommendation(plan: IterationPlan, response: Mapping[str, Any], *, protocol=None) -> dict[str, Any]:
    response = mapping(dict(response), "reviewer.response")
    for key in ("reviewer_id", "role", "decision"):
        text(response.get(key), f"reviewer.response.{key}")
    decision = response["decision"]
    if decision not in {"adjust", "hold", "unavailable", "invalid"}:
        raise Invalid("reviewer.response.decision", "decisão desconhecida")
    result = {key: copy.deepcopy(response[key]) for key in response}
    if decision == "adjust":
        if "adjustment" not in response:
            raise Invalid("reviewer.response.adjustment", "ajuste ausente")
        result["adjustment"] = validate_adjustment(plan, response["adjustment"], protocol=protocol)
    elif "adjustment" in response:
        raise Invalid("reviewer.response.adjustment", "hold não pode carregar ajuste")
    return result


def collect_critiques(plan: IterationPlan, evidence: Mapping[str, Any], *, reviewers: Mapping[str, Reviewer] | None = None,
                      journal: IterationJournal | None = None, round: int | None = None, only_role=None) -> tuple[dict[str, Any], ...]:
    # Evidence is copied separately for every reviewer; no response is inserted
    # into the package until all isolated calls have returned.
    approved = copy.deepcopy(dict(evidence))
    approved.pop("final", None)
    approved.pop("confirmation", None)
    protocol = evidence.get("protocol")
    if isinstance(protocol, dict) and "experiments" in protocol:
        protocol = {"nodes": protocol.get("experiments", {}).get(plan.experiment_id, {}).get("nodes", {})}
    responses: list[dict[str, Any]] = []
    for spec in plan.reviewers:
        if only_role is not None and spec.role != only_role:
            continue
        try:
            adapter = _reviewer_from_spec(spec, reviewers)
            response = adapter.review(copy.deepcopy(approved))
            validated = validate_recommendation(plan, response, protocol=protocol)
            if validated.get("role") != spec.role:
                raise Invalid("reviewer.response.role", "papel da resposta difere do crítico selecionado")
            expected_id = spec.config.get("reviewer_id", f"{spec.backend}:{spec.role}")
            if validated.get("reviewer_id") != expected_id:
                raise Invalid("reviewer.response.reviewer_id", "identidade da resposta difere do crítico selecionado")
        except ReviewerUnavailable as exc:
            validated = {"reviewer_id": f"unavailable:{spec.role}", "role": spec.role,
                         "decision": "unavailable", "error": str(exc)}
        except (Invalid, TypeError, ValueError, KeyError) as exc:
            validated = {"reviewer_id": f"invalid:{spec.role}", "role": spec.role,
                         "decision": "invalid", "error": str(exc)}
        responses.append(validated)
        if journal is not None:
            journal.event("critic_opinion", round=round, payload=validated)
    return tuple(responses)


def reconcile_critiques(opinions: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    unavailable = [op for op in opinions if op.get("decision") in {"unavailable", "invalid"}]
    adjustments = [op["adjustment"] for op in opinions if op.get("decision") == "adjust" and "adjustment" in op]
    unique = {_hash(item): item for item in adjustments}
    disagreements = []
    if len(unique) > 1:
        disagreements = [dict(op) for op in opinions]
    if unavailable:
        return {"status": "blocked", "reason": "critic_unavailable_or_invalid", "disagreements": disagreements,
                "opinions": [dict(op) for op in opinions]}
    if disagreements:
        return {"status": "blocked", "reason": "conflicting_recommendations", "disagreements": disagreements,
                "opinions": [dict(op) for op in opinions]}
    if not unique:
        return {"status": "stop", "reason": "no_eligible_adjustment", "disagreements": [],
                "opinions": [dict(op) for op in opinions]}
    adjustment = next(iter(unique.values()))
    return {"status": "candidate", "adjustment": adjustment, "disagreements": [],
            "opinions": [dict(op) for op in opinions]}


# ---------------------------------------------------------------------------
# Development rounds


def _read_protocol(lease, proposal_id: str) -> tuple[dict[str, Any], bytes, bytes]:
    directory = lease.active_directory()
    proposal_path = directory / artifact_paths()["proposal"]
    protocol_path = directory / artifact_paths()["protocol"]
    proposal_raw = read_limited(proposal_path)
    protocol_raw = read_limited(protocol_path)
    return yaml.safe_load(protocol_raw), proposal_raw, protocol_raw


def _run_manifest_path(lease, run_id: str) -> Path:
    return lease.active_directory() / "runs" / run_id / Path(artifact_paths()["run"]).name


def _run_exists(lease, run_id: str) -> bool:
    path = _run_manifest_path(lease, run_id)
    return path.is_file() and not path.is_symlink()


def _find_published_run(lease, iteration_id: str, round_no: int) -> dict[str, Any] | None:
    runs = lease.active_directory() / "runs"
    if not runs.exists() or not runs.is_dir():
        return None
    for folder in sorted(runs.iterdir()):
        manifest = folder / Path(artifact_paths()["run"]).name
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            receipt = yaml.safe_load(manifest.read_bytes())
        except (OSError, yaml.YAMLError):
            continue
        if receipt.get("iteration_id") == iteration_id and receipt.get("phase") == "development" and receipt.get("round") == round_no:
            return {"run_id": receipt.get("run_id", folder.name), "adjustment": receipt.get("adjustment")}
    return None


def _round_evidence(receipt: Mapping[str, Any], protocol: Mapping[str, Any], experiment) -> dict[str, Any]:
    return {
        "run_id": receipt["run_id"], "phase": "development", "metrics": copy.deepcopy(receipt.get("metrics", {})),
        "verdict": receipt.get("verdict"), "checks": copy.deepcopy(receipt.get("checks", {})),
        "protocol": copy.deepcopy(protocol), "experiment": experiment.as_dict(),
        "input_provenance": {"snapshot": copy.deepcopy(receipt.get("snapshot", {})),
                             "definition_hashes": copy.deepcopy(receipt.get("definition_hashes", {}))},
    }


def _publish_development(lease, validation, experiment, execution, *, iteration_id, round_no, adjustment=None):
    from .evidence import publish_run
    return publish_run(lease, validation, experiment, execution, metadata={
        "iteration_id": iteration_id, "phase": "development", "round": round_no,
        "adjustment": copy.deepcopy(adjustment), "input_provenance": {
            "snapshot": dict(experiment.snapshot), "windows": {k: v.as_dict() for k, v in experiment.windows.items()},
        },
    })


def _find_latest_round(journal: IterationJournal) -> dict[str, Any] | None:
    events = journal.events()
    for event in reversed(events):
        if event["kind"] == "development_run":
            return event["payload"]
    return None


def _iteration_protocol(lease, proposal_id, experiment_id, adjustment=None):
    proposal_path = lease.active_directory() / artifact_paths()["proposal"]
    protocol_path = lease.active_directory() / artifact_paths()["protocol"]
    proposal_raw, protocol_raw = read_limited(proposal_path), read_limited(protocol_path)
    protocol = yaml.safe_load(protocol_raw)
    if adjustment is not None:
        protocol = _apply_adjustment(protocol, experiment_id, adjustment)
        protocol_raw = yaml.safe_dump(protocol, sort_keys=False, allow_unicode=True).encode()
    return proposal_raw, protocol_raw, protocol


def _execute_round(lease, proposal_id: str, experiment_id: str, *, iteration_id: str, round_no: int,
                   adjustment: Mapping[str, Any] | None = None, data_root=None) -> dict[str, Any]:
    proposal_raw, protocol_raw, protocol = _iteration_protocol(lease, proposal_id, experiment_id, adjustment)
    validation = validate_documents(proposal_raw, protocol_raw, expected_id=proposal_id, workspace=lease.workspace)
    if not validation.valid():
        issue = validation.issues[0]
        raise Invalid(issue.path, issue.message)
    experiment = validation.definition.experiments[experiment_id]
    from .runner import execute_experiment
    execution = execute_experiment(experiment, lease.workspace.effective_data_root(data_root))
    receipt = _publish_development(lease, validation, experiment, execution, iteration_id=iteration_id,
                                   round_no=round_no, adjustment=adjustment)
    return {"receipt": receipt, "validation": validation, "experiment": experiment, "protocol": protocol}


def _iteration_data_root(plan: IterationPlan, workspace: Workspace, data_root=None):
    return workspace.effective_data_root(data_root)


def _update_manifest(directory: Path, *, status: str, round_no: int, checkpoint: Mapping[str, Any] | None = None,
                     confirmation_consumed: Mapping[str, Any] | None = None):
    path = directory / ITERATION_NAME
    data = yaml.safe_load(path.read_bytes())
    data["status"], data["round"] = status, round_no
    if checkpoint is not None:
        data["checkpoint"] = dict(checkpoint)
    if confirmation_consumed is not None:
        data["confirmation_consumed"] = dict(confirmation_consumed)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(temporary, path)


def _legacy_dispatch_barrier(lease, plan):
    state = StudyState(lease, plan.experiment_id)
    if state.exists:
        raise Invalid("study.owner", f"trilha gerenciada pertence a {state.owner}; iteração legada é somente leitura")


def submit_iteration_review(root, proposal_id, iteration_id, path, *, expected_revision=None):
    from .managed_iteration import review_iteration_round

    with proposal_guard(root, proposal_id) as lease:
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is None:
            raise Invalid("study", "revisão de rodada exige iteração gerenciada")
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            return review_iteration_round(lease, directory, plan, journal, path, expected_revision=expected_revision)


def retry_iteration_round(root, proposal_id, iteration_id, path, *, data_root=None, expected_revision=None):
    from .managed_iteration import retry_round

    with proposal_guard(root, proposal_id) as lease:
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is None:
            raise Invalid("study", "nova tentativa exige iteração gerenciada")
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            return retry_round(lease, directory, plan, journal, path, data_root=data_root,
                               expected_revision=expected_revision)


def resume_iteration(root, proposal_id: str, iteration_id: str, *, data_root=None,
                     reviewers: Mapping[str, Reviewer] | None = None, expected_revision=None) -> dict[str, Any]:
    with proposal_guard(root, proposal_id) as lease:
        _complete_pending_creation(lease, iteration_id)
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is not None:
            from .managed_iteration import advance

            with IterationJournal(directory / JOURNAL_NAME) as journal:
                return advance(lease, directory, plan, journal, data_root=data_root, reviewers=reviewers,
                               expected_revision=expected_revision)
        _legacy_dispatch_barrier(lease, plan)
        if manifest.get("status") in {"completed", "archived", "blocked", "stopped", "candidate_frozen", "confirmation_approved", "confirmation_failed"}:
            return inspect_iteration(lease.workspace.root, proposal_id, iteration_id)
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            checkpoint = journal.checkpoints()
            round_no = int(checkpoint.get("round", 0))
            latest = _find_latest_round(journal)
            if latest is None:
                dispatch = checkpoint.get("dispatch")
                if dispatch:
                    reconciled = _find_published_run(lease, iteration_id, int(dispatch.get("round", -1)))
                    if reconciled is not None:
                        journal.event("round_reconciled", round=dispatch.get("round"), payload=reconciled)
                        journal.event("development_run", round=dispatch.get("round"), payload=reconciled)
                        journal.checkpoint("round", int(dispatch.get("round", 0)))
                        journal.checkpoint("last_run_id", reconciled["run_id"])
                        journal.checkpoint("dispatch", None)
                        latest = reconciled
                    else:
                        journal.event("round_unknown", round=dispatch.get("round"), payload={"reason": "dispatch sem recibo"})
                        journal.checkpoint("status", "blocked")
                        _update_manifest(directory, status="blocked", round_no=round_no)
                        return inspect_iteration(lease.workspace.root, proposal_id, iteration_id)
            if latest is None:
                if not journal.reserve("rounds", 1, limit=plan.max_rounds):
                    journal.checkpoint("status", "stopped")
                    journal.event("stopped", payload={"reason": "budget_exhausted"})
                    _update_manifest(directory, status="stopped", round_no=round_no)
                else:
                    journal.consume("rounds")
                    journal.checkpoint("dispatch", {"phase": "development", "round": 0})
                    journal.event("round_reserved", round=0, payload={"phase": "development", "round": 0})
                    try:
                        result = _execute_round(lease, proposal_id, plan.experiment_id, iteration_id=iteration_id,
                                                round_no=0, data_root=data_root)
                    except Exception as exc:
                        reconciled = _find_published_run(lease, iteration_id, 0)
                        if reconciled is None:
                            journal.event("round_unknown", round=0, payload={"error": str(exc)})
                            journal.checkpoint("status", "blocked")
                            _update_manifest(directory, status="blocked", round_no=round_no)
                            raise
                        result = None
                        journal.event("round_reconciled", round=0, payload=reconciled)
                        journal.event("development_run", round=0, payload=reconciled)
                        receipt = None
                    if result is not None:
                        receipt = result["receipt"]
                        journal.event("development_run", round=0, payload={"run_id": receipt["run_id"], "adjustment": None})
                    elif receipt is None:
                        receipt = yaml.safe_load(_run_manifest_path(lease, reconciled["run_id"]).read_bytes())
                    journal.checkpoint("dispatch", None)
                    journal.checkpoint("round", 0)
                    journal.checkpoint("last_run_id", receipt["run_id"])
                    latest = {"run_id": receipt["run_id"], "adjustment": None}
            # A published run may exist while the checkpoint write was interrupted.
            if latest and not _run_exists(lease, latest["run_id"]):
                raise Invalid("iteration", "checkpoint aponta para uma execução ausente")
            if latest and checkpoint.get("status") not in {"awaiting_approval", "blocked", "stopped"}:
                manifest_path = _run_manifest_path(lease, latest["run_id"])
                receipt = yaml.safe_load(manifest_path.read_bytes())
                if receipt.get("verdict") == "passed" and plan.stop.get("on_pass", False):
                    journal.checkpoint("status", "stopped")
                    journal.event("stopped", round=round_no, payload={"reason": "criterion_pass", "run_id": receipt.get("run_id")})
                    _update_manifest(directory, status="stopped", round_no=round_no)
                    return inspect_iteration(lease.workspace.root, proposal_id, iteration_id)
                protocol = yaml.safe_load((lease.active_directory() / artifact_paths()["protocol"]).read_bytes())
                evidence = _round_evidence(receipt, protocol, validate_proposal(lease.workspace, proposal_id).definition.experiments[plan.experiment_id])
                if not journal.reserve("reviewer_calls", len(plan.reviewers), limit=plan.max_reviewer_calls):
                    journal.checkpoint("status", "stopped")
                    journal.event("stopped", round=round_no, payload={"reason": "reviewer_budget_exhausted"})
                    _update_manifest(directory, status="stopped", round_no=round_no)
                else:
                    journal.consume("reviewer_calls", len(plan.reviewers))
                    opinions = collect_critiques(plan, evidence, reviewers=reviewers, journal=journal, round=round_no)
                    reconciliation = reconcile_critiques(opinions)
                    journal.event("reconciled", round=round_no, payload=reconciliation)
                    if reconciliation["status"] == "candidate":
                        adjustment = reconciliation["adjustment"]
                        protocol = yaml.safe_load((lease.active_directory() / artifact_paths()["protocol"]).read_bytes())
                        try:
                            adjustment = validate_adjustment(plan, adjustment, protocol={"nodes": protocol["experiments"][plan.experiment_id]["nodes"]})
                        except Invalid as exc:
                            journal.event("adjustment_rejected", round=round_no, payload={"reason": str(exc), "adjustment": adjustment})
                            journal.checkpoint("status", "blocked")
                            _update_manifest(directory, status="blocked", round_no=round_no)
                        else:
                            a_hash = adjustment_hash(adjustment)
                            if journal.identity("adjustment:" + a_hash) is not None:
                                journal.event("adjustment_duplicate", round=round_no, payload={"adjustment_hash": a_hash})
                                journal.checkpoint("status", "stopped")
                                _update_manifest(directory, status="stopped", round_no=round_no)
                            else:
                                journal.identity("adjustment:" + a_hash, {"adjustment": adjustment, "round": round_no})
                                journal.checkpoint("pending_adjustment", adjustment)
                                journal.checkpoint("status", "awaiting_approval")
                                journal.event("approval_required", round=round_no, payload={"adjustment": adjustment, "adjustment_hash": a_hash})
                                _update_manifest(directory, status="awaiting_approval", round_no=round_no, checkpoint={"pending_adjustment": adjustment})
                    elif reconciliation["status"] == "blocked":
                        journal.checkpoint("status", "blocked")
                        journal.event("blocked", round=round_no, payload={"reason": reconciliation["reason"]})
                        _update_manifest(directory, status="blocked", round_no=round_no)
                    else:
                        journal.checkpoint("status", "stopped")
                        journal.event("stopped", round=round_no, payload={"reason": reconciliation["reason"]})
                        _update_manifest(directory, status="stopped", round_no=round_no)
            status = journal.checkpoints().get("status", "paused")
            if status == "awaiting_approval" and plan.automatic:
                status = _approve_and_run_locked(lease, directory, plan, journal, data_root=data_root)
                _update_manifest(directory, status=status, round_no=int(journal.checkpoints().get("round", 0)))
    return inspect_iteration(root, proposal_id, iteration_id)


def _approve_and_run_locked(lease, directory, plan, journal, *, data_root=None, adjustment=None, approved_by="plan:automatic"):
    if adjustment is None:
        adjustment = journal.checkpoints().get("pending_adjustment")
    if not adjustment:
        raise Invalid("adjustment", "não há ajuste pendente")
    base_protocol = yaml.safe_load((lease.active_directory() / artifact_paths()["protocol"]).read_bytes())
    adjustment = validate_adjustment(plan, adjustment, protocol={"nodes": base_protocol["experiments"][plan.experiment_id]["nodes"]})
    a_hash = adjustment_hash(adjustment)
    if journal.identity("approval:" + a_hash) is not None:
        raise Invalid("approval", "ajuste já aprovado")
    current_round = int(journal.checkpoints().get("round", 0))
    if current_round + 1 >= plan.max_rounds:
        journal.checkpoint("status", "stopped")
        journal.event("stopped", round=current_round, payload={"reason": "budget_exhausted"})
        return "stopped"
    if not journal.reserve("rounds", 1, limit=plan.max_rounds):
        journal.checkpoint("status", "stopped")
        journal.event("stopped", round=current_round, payload={"reason": "budget_exhausted"})
        return "stopped"
    journal.consume("rounds")
    journal.identity("approval:" + a_hash, {"adjustment": adjustment, "approved_by": approved_by})
    next_round = current_round + 1
    journal.event("adjustment_approved", round=next_round, payload={"adjustment": adjustment, "adjustment_hash": a_hash, "approved_by": approved_by})
    journal.checkpoint("dispatch", {"phase": "development", "round": next_round, "adjustment": adjustment})
    try:
        result = _execute_round(lease, plan.proposal_id, plan.experiment_id, iteration_id=journal.identity("iteration_id"),
                                round_no=next_round, adjustment=adjustment, data_root=data_root)
    except Exception as exc:
        reconciled = _find_published_run(lease, journal.identity("iteration_id"), next_round)
        if reconciled is None:
            journal.event("round_unknown", round=next_round, payload={"error": str(exc), "adjustment": adjustment})
            journal.checkpoint("status", "blocked")
            raise
        journal.event("round_reconciled", round=next_round, payload=reconciled)
        journal.event("development_run", round=next_round, payload={**reconciled, "adjustment": adjustment, "adjustment_hash": a_hash})
        receipt = yaml.safe_load(_run_manifest_path(lease, reconciled["run_id"]).read_bytes())
    else:
        receipt = result["receipt"]
        journal.event("development_run", round=next_round, payload={"run_id": receipt["run_id"], "adjustment": adjustment, "adjustment_hash": a_hash})
    journal.checkpoint("dispatch", None)
    journal.checkpoint("round", next_round)
    journal.checkpoint("last_run_id", receipt["run_id"])
    journal.checkpoint("pending_adjustment", None)
    journal.checkpoint("status", "paused")
    return "paused"


def approve_adjustment(root, proposal_id: str, iteration_id: str, adjustment: Mapping[str, Any] | None = None, *, data_root=None,
                       approved_by="human", expected_revision=None) -> dict[str, Any]:
    with proposal_guard(root, proposal_id) as lease:
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is not None:
            from .managed_iteration import approve

            with IterationJournal(directory / JOURNAL_NAME) as journal:
                return approve(lease, directory, plan, journal, adjustment, data_root=data_root,
                               expected_revision=expected_revision)
        _legacy_dispatch_barrier(lease, plan)
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            checkpoint = journal.checkpoints()
            pending = adjustment or checkpoint.get("pending_adjustment")
            if pending is None:
                raise Invalid("adjustment", "não há ajuste pendente")
            expected = checkpoint.get("pending_adjustment")
            if expected is not None and adjustment is not None and _hash(expected) != _hash(adjustment):
                raise Invalid("approval", "aprovação não corresponde ao ajuste pendente")
            status = _approve_and_run_locked(lease, directory, plan, journal, data_root=data_root,
                                             adjustment=pending, approved_by=approved_by)
            _update_manifest(directory, status=status, round_no=int(journal.checkpoints().get("round", 0)))
    return inspect_iteration(root, proposal_id, iteration_id)


# ---------------------------------------------------------------------------
# Confirmation reservation and final scoring


def _reservation_files(root: Path):
    directory = root / FINAL_RESERVATIONS
    if directory.is_symlink():
        raise Invalid("confirmation", "diretório de reservas não pode ser symlink")
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise Invalid("confirmation", "esperado diretório de reservas")
    return directory.glob("*.json")


def _known_exposures(root: Path, lineage: str, *, strict=False) -> list[tuple[Window, dict[str, Any]]]:
    root = Path(root).resolve()
    result = []
    for path in _reservation_files(root):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("reserva não é arquivo regular")
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or not isinstance(data.get("lineage"), str):
                raise ValueError("reserva sem linhagem")
            window = _window(data["window"], "reservation.window")
            if data["lineage"] == lineage:
                result.append((window, data))
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            if strict:
                raise Invalid("confirmation.reservation", f"reserva não verificável: {path.name}") from exc
    for base in evidence_directories(root):
        if not base.exists():
            continue
        for iteration in base.glob("*/iterations/*/iteration.yaml"):
            try:
                data = yaml.safe_load(iteration.read_bytes())
                consumed = data.get("confirmation_consumed", {})
                if consumed.get("lineage") == lineage:
                    result.append((_window(consumed["window"], "confirmation.window"), consumed))
                journal_path = iteration.parent / JOURNAL_NAME
                if journal_path.is_file() and not journal_path.is_symlink():
                    uri = f"file:{journal_path.resolve()}?mode=ro"
                    with closing(sqlite3.connect(uri, uri=True)) as connection:
                        row = connection.execute("SELECT value FROM checkpoints WHERE key='confirmation_consumed'").fetchone()
                    if row:
                        consumed = json.loads(row[0])
                        if consumed.get("lineage") == lineage:
                            result.append((_window(consumed["window"], "confirmation.window"), consumed))
            except (OSError, TypeError, KeyError, ValueError, yaml.YAMLError, sqlite3.Error, json.JSONDecodeError):
                continue
    return result


def validate_final_partition(root, plan: IterationPlan) -> dict[str, Any]:
    if plan.confirmation_window.start < plan.development_windows["evaluation"].end:
        raise Invalid("confirmation.window", "confirmação cruza avaliação de desenvolvimento")
    with workspace_layout_guard(root):
        projects_directory(root)
        with partition_lock(Path(root).resolve()):
            for window, details in _known_exposures(Path(root).resolve(), plan.confirmation_lineage):
                if overlap(window, plan.confirmation_window):
                    raise Invalid("confirmation.window", "intervalo já exposto para a mesma linhagem")
            if any(overlap(window, plan.confirmation_window)
                   for window in development_exposures(root, plan.confirmation_lineage)):
                raise Invalid("confirmation.window", "intervalo já exposto pelo desenvolvimento na mesma linhagem")
    return {"lineage": plan.confirmation_lineage, "window": plan.confirmation_window.as_dict(), "eligible": True}


def _reserve_final(lease, directory: Path, plan: IterationPlan, journal: IterationJournal, *, footprint=None) -> Path:
    root = lease.workspace.root
    validate_final_partition(root, plan)
    exposed = footprint or plan.confirmation_window
    token = _hash({"lineage": plan.confirmation_lineage, "window": exposed.as_dict()})
    reservation = root / FINAL_RESERVATIONS / f"{token}.json"
    reservation.parent.mkdir(exist_ok=True)
    with partition_lock(root):
        for window, _ in _known_exposures(root, plan.confirmation_lineage, strict=footprint is not None):
            if overlap(window, exposed):
                raise Invalid("confirmation.window", "intervalo já reservado ou exposto")
        if any(overlap(window, exposed)
               for window in development_exposures(root, plan.confirmation_lineage)):
            raise Invalid("confirmation.window", "intervalo já exposto pelo desenvolvimento na mesma linhagem")
        payload = {"reservation_id": token, "lineage": plan.confirmation_lineage,
                   "window": exposed.as_dict(), "evaluation_window": plan.confirmation_window.as_dict(),
                   "iteration_id": journal.identity("iteration_id"), "proposal": plan.proposal_id,
                   "reserved_at": _timestamp(), "status": "reserved"}
        if reservation.exists() or reservation.is_symlink():
            raise Invalid("confirmation.window", "intervalo já reservado ou exposto")
        write_durable_json(reservation, payload)
    journal.event("confirmation_reserved", phase="confirmation", payload={**payload, "path": str(reservation)})
    journal.checkpoint("confirmation_reservation", str(reservation))
    return reservation


def freeze_candidate(root, proposal_id: str, iteration_id: str, *, run_id: str | None = None, expected_revision=None) -> dict[str, Any]:
    with proposal_guard(root, proposal_id) as lease:
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is not None:
            from .managed_iteration import freeze

            with IterationJournal(directory / JOURNAL_NAME) as journal:
                return freeze(lease, directory, plan, journal, run_id, expected_revision=expected_revision)
        _legacy_dispatch_barrier(lease, plan)
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            events = journal.events()
            runs = [event["payload"] for event in events if event["kind"] == "development_run" and event["payload"].get("run_id")]
            if run_id is not None:
                selected = next((item for item in runs if item["run_id"] == run_id), None)
                if selected is None:
                    raise Invalid("candidate", "run de desenvolvimento não encontrado")
            else:
                # The first frozen comparison is also a valid candidate: a
                # plan may deliberately test the proposal's initial candidate
                # without requiring an adaptive adjustment round.
                selected = next((item for item in reversed(runs) if item.get("adjustment") is not None), None)
                if selected is None:
                    selected = next(reversed(runs), None)
            if selected is None:
                raise Invalid("candidate", "nenhum candidato de desenvolvimento disponível")
            frozen = {"run_id": selected["run_id"], "adjustment": selected.get("adjustment"),
                      "adjustment_hash": selected.get("adjustment_hash", adjustment_hash(selected["adjustment"])),
                      "frozen_at": _timestamp()}
            previous = journal.checkpoint("candidate")
            if previous is not None and _hash(previous) != _hash(frozen):
                raise Invalid("candidate", "a iteração já congelou outro candidato")
            journal.checkpoint("candidate", frozen)
            journal.event("candidate_frozen", phase="development", round=journal.checkpoints().get("round"), payload=frozen)
            _update_manifest(directory, status="candidate_frozen", round_no=int(journal.checkpoints().get("round", 0)), checkpoint={"candidate": frozen})
    return inspect_iteration(root, proposal_id, iteration_id)


def approve_confirmation(root, proposal_id: str, iteration_id: str, *, approved_by="human", data_root=None, expected_revision=None) -> dict[str, Any]:
    with proposal_guard(root, proposal_id) as lease:
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is None:
            _legacy_dispatch_barrier(lease, plan)
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            if plan.initial_study is not None:
                from .managed_iteration import verify_frozen

                verify_frozen(lease, directory, plan, journal, expected_revision)
            candidate = journal.checkpoint("candidate")
            if not candidate:
                raise Invalid("confirmation", "nenhum candidato congelado")
            if journal.checkpoint("confirmation_approval") is not None:
                raise Invalid("confirmation", "confirmação já autorizada")
            journal.checkpoint("confirmation_approval", {"approved_by": approved_by, "at": _timestamp()})
            journal.checkpoint("status", "confirmation_approved")
            journal.event("confirmation_approved", phase="confirmation", payload={"approved_by": approved_by})
            _update_manifest(directory, status="confirmation_approved", round_no=int(journal.checkpoints().get("round", 0)))
            return inspect_iteration(lease.workspace.root, proposal_id, iteration_id)


def _prepare_confirmation(lease, plan: IterationPlan, candidate: Mapping[str, Any], *, data_root=None):
    """Prepare the frozen arms using only the development snapshot.

    The returned execution is intentionally kept in memory.  It is a proof that
    preparation completed before the final reservation is consumed; no final
    manifest or payload is touched by this helper.
    """
    from .runner import execute_experiment
    from .snapshots import load_snapshot, read_snapshot_manifest

    proposal_path = lease.active_directory() / artifact_paths()["proposal"]
    protocol_path = lease.active_directory() / artifact_paths()["protocol"]
    proposal_raw = read_limited(proposal_path)
    protocol = yaml.safe_load(read_limited(protocol_path))
    for adjustment in candidate.get("adjustments", [candidate.get("adjustment")]):
        protocol = _apply_adjustment(protocol, plan.experiment_id, adjustment)
    validation = validate_documents(proposal_raw, yaml.safe_dump(protocol, sort_keys=False, allow_unicode=True).encode(), expected_id=plan.proposal_id, workspace=lease.workspace)
    if not validation.valid():
        issue = validation.issues[0]
        raise Invalid(issue.path, issue.message)
    experiment = validation.definition.experiments[plan.experiment_id]
    from .graph import require_supported_route

    require_supported_route(experiment)
    root = lease.workspace.effective_data_root(data_root)
    if experiment.managed_lineage is not None:
        from .managed import digest

        if digest(experiment.as_dict()) != candidate["effective_hash"]:
            raise Invalid("confirmation", "configuração preparada diverge do candidato congelado")
        manifest = read_snapshot_manifest(root, experiment.snapshot)
        verify_managed_span(manifest, experiment.managed_lineage, experiment.windows, "protocol.windows")
        if plan.confirmation_window.start < manifest.span.end:
            raise Invalid("confirmation.window", "confirmação precisa ocorrer após o fim do snapshot de desenvolvimento")
        record_development_exposure(lease, experiment, manifest)
        verified = load_snapshot(root, experiment.snapshot, manifest=manifest)
        execution = execute_experiment(experiment, root, verified_snapshot=verified)
    else:
        execution = execute_experiment(experiment, root)
    return validation, experiment, execution


def _confirm_locked(lease, directory: Path, plan: IterationPlan, journal: IterationJournal,
                    candidate: Mapping[str, Any], *, data_root=None) -> dict[str, Any]:
    if journal.checkpoint("confirmation_consumed") is not None:
        raise Invalid("confirmation", "confirmação já consumida; não é permitido replay")
    try:
        prepared_validation, prepared_experiment, prepared_execution = _prepare_confirmation(
            lease, plan, candidate, data_root=data_root
        )
        final_manifest = None
        if plan.initial_study is not None:
            from .snapshots import read_snapshot_manifest

            final_manifest = read_snapshot_manifest(lease.workspace.effective_data_root(data_root),
                                                    plan.confirmation_snapshot)
            verify_managed_span(final_manifest, prepared_experiment.managed_lineage,
                                {"window": plan.confirmation_window}, "confirmation")
            if final_manifest.span.start < prepared_execution.snapshot.span.end:
                raise Invalid("confirmation.snapshot", "snapshot final contém período de desenvolvimento")
    except Exception as exc:
        journal.event("confirmation_preparation_failed", phase="confirmation", payload={"error": str(exc)})
        journal.checkpoint("status", "confirmation_failed")
        _update_manifest(directory, status="confirmation_failed", round_no=int(journal.checkpoints().get("round", 0)))
        raise
    journal.event("confirmation_prepared", phase="confirmation", payload={
        "candidate_run_id": candidate["run_id"], "development_snapshot": dict(plan.development_snapshot),
        "training": copy.deepcopy(prepared_execution.training),
    })
    reservation = _reserve_final(lease, directory, plan, journal,
                                 footprint=final_manifest.span if final_manifest is not None else None)
    # The durable transition is committed before any final snapshot is opened.
    consumed = {"reservation_id": reservation.stem, "lineage": plan.confirmation_lineage,
                "window": {"start": plan.confirmation_window.start.isoformat().replace("+00:00", "Z"),
                           "end": plan.confirmation_window.end.isoformat().replace("+00:00", "Z")},
                "at": _timestamp()}
    journal.checkpoint("confirmation_consumed", consumed)
    _update_manifest(directory, status="confirmation_consumed", round_no=int(journal.checkpoints().get("round", 0)),
                     confirmation_consumed=consumed)
    journal.event("confirmation_access", phase="confirmation", payload=consumed)
    reservation_data = json.loads(reservation.read_bytes())
    reservation_data["status"] = "consumed"
    write_durable_json(reservation, reservation_data)
    try:
        # Apply the prepared development state to the final evaluation only;
        # the final payload is first loaded after the durable access event.
        proposal_path = lease.active_directory() / artifact_paths()["proposal"]
        protocol_path = lease.active_directory() / artifact_paths()["protocol"]
        proposal_raw = read_limited(proposal_path)
        protocol = yaml.safe_load(read_limited(protocol_path))
        for adjustment in candidate.get("adjustments", [candidate.get("adjustment")]):
            protocol = _apply_adjustment(protocol, plan.experiment_id, adjustment)
        exp_doc = protocol["experiments"][plan.experiment_id]
        exp_doc["snapshot"] = copy.deepcopy(plan.confirmation_snapshot)
        exp_doc["windows"] = {
            "train": {k: v for k, v in plan.development_windows["train"].as_dict().items()},
            "calibration": {k: v for k, v in plan.development_windows["calibration"].as_dict().items()},
            "evaluation": {k: v for k, v in plan.confirmation_window.as_dict().items()},
        }
        validation = validate_documents(proposal_raw, yaml.safe_dump(protocol, sort_keys=False, allow_unicode=True).encode(), expected_id=plan.proposal_id, workspace=lease.workspace)
        if not validation.valid():
            issue = validation.issues[0]
            raise Invalid(issue.path, issue.message)
        experiment = validation.definition.experiments[plan.experiment_id]
        from .evidence import publish_run
        from .runner import execute_prepared_comparison
        options = {"verified_manifest": final_manifest} if final_manifest is not None else {}
        execution = execute_prepared_comparison(
            experiment, lease.workspace.effective_data_root(data_root),
            getattr(prepared_execution, "state", {}), **options,
        )
        receipt = publish_run(lease, validation, experiment, execution, metadata={
            "iteration_id": journal.identity("iteration_id"), "phase": "confirmation",
            "round": journal.checkpoints().get("round"), "candidate_run_id": candidate["run_id"],
            "input_provenance": {"development_snapshot": dict(plan.development_snapshot),
                                 "prepared_training": copy.deepcopy(prepared_execution.training),
                                 "confirmation_snapshot": dict(plan.confirmation_snapshot),
                                 "confirmation_window": plan.confirmation_window.as_dict(),
                                 "reservation_id": reservation.stem},
        })
    except Exception as exc:
        journal.event("confirmation_failed", phase="confirmation", payload={"error": str(exc)})
        journal.checkpoint("status", "confirmation_failed")
        _update_manifest(directory, status="confirmation_failed", round_no=int(journal.checkpoints().get("round", 0)))
        raise
    journal.event("confirmation_run", phase="confirmation", payload={"run_id": receipt["run_id"], "verdict": receipt["verdict"]})
    journal.checkpoint("confirmation_run_id", receipt["run_id"])
    journal.checkpoint("status", "completed")
    journal.checkpoint("confirmation_run_id", receipt["run_id"])
    _update_manifest(directory, status="completed", round_no=int(journal.checkpoints().get("round", 0)), checkpoint={"confirmation_run_id": receipt["run_id"]})
    return inspect_iteration(lease.workspace.root, plan.proposal_id, journal.identity("iteration_id"))


def confirm_iteration(root, proposal_id: str, iteration_id: str, *, data_root=None, expected_revision=None) -> dict[str, Any]:
    with proposal_guard(root, proposal_id) as lease:
        directory, manifest, plan = _load_iteration(lease.workspace.root, proposal_id, iteration_id)
        _ensure_frozen_definition(lease, proposal_id, manifest)
        if plan.initial_study is None:
            _legacy_dispatch_barrier(lease, plan)
        with IterationJournal(directory / JOURNAL_NAME) as journal:
            if plan.initial_study is not None:
                from .managed_iteration import verify_frozen

                verify_frozen(lease, directory, plan, journal, expected_revision)
                if data_root is None:
                    data_root = journal.checkpoint("managed_data_root")
            candidate = journal.checkpoint("candidate")
            if not candidate:
                raise Invalid("confirmation", "nenhum candidato congelado")
            if journal.checkpoint("confirmation_approval") is None:
                raise Invalid("confirmation", "confirmação exige aprovação separada")
            return _confirm_locked(lease, directory, plan, journal, candidate, data_root=data_root)


def cli_iterate(args) -> int:
    iteration_id = args.iteration
    try:
        operations = [args.resume, args.approve_adjustment, args.freeze_candidate, args.approve_confirmation,
                      args.confirm, args.diagnose, args.submit_review, args.retry_study]
        if sum(map(bool, operations)) > 1 or (args.plan and any(operations[1:])):
            raise Invalid("iterate", "informe apenas uma ação para a iteração")
        if args.plan:
            if args.state_revision is not None:
                raise Invalid("iterate", "não use --state-revision ao criar uma iteração")
            result = create_iteration(args.root, args.proposal, args.experiment, args.plan,
                                      iteration_id=iteration_id, data_root=args.data_root)
            iteration_id = result["iteration_id"]
            if args.resume:
                result = resume_iteration(args.root, args.proposal, iteration_id, data_root=args.data_root)
        elif not iteration_id:
            raise Invalid("iterate", "informe --plan para criar ou --iteration para consultar e avançar")
        elif args.resume:
            result = resume_iteration(args.root, args.proposal, iteration_id, data_root=args.data_root,
                                      expected_revision=args.state_revision)
        elif args.retry_study:
            result = retry_iteration_round(args.root, args.proposal, iteration_id, args.retry_study,
                                           data_root=args.data_root, expected_revision=args.state_revision)
        elif args.submit_review:
            result = submit_iteration_review(args.root, args.proposal, iteration_id, args.submit_review,
                                             expected_revision=args.state_revision)
        elif args.approve_adjustment:
            adjustment = json.loads(args.approve_adjustment)
            result = approve_adjustment(args.root, args.proposal, iteration_id, adjustment, data_root=args.data_root,
                                        expected_revision=args.state_revision)
        elif args.freeze_candidate:
            result = freeze_candidate(args.root, args.proposal, iteration_id,
                                      run_id=args.freeze_candidate if args.freeze_candidate != "true" else None,
                                      expected_revision=args.state_revision)
        elif args.approve_confirmation:
            result = approve_confirmation(args.root, args.proposal, iteration_id, data_root=args.data_root,
                                          expected_revision=args.state_revision)
        elif args.confirm:
            result = confirm_iteration(args.root, args.proposal, iteration_id, data_root=args.data_root,
                                        expected_revision=args.state_revision)
        else:
            result = inspect_iteration(args.root, args.proposal, iteration_id)
            if args.diagnose:
                from .managed_iteration import diagnose_iteration_round

                if "schema_version" not in result:
                    raise Invalid("study", "diagnóstico de rodada exige iteração gerenciada")
                result = diagnose_iteration_round(args.root, args.proposal, result)
    except (OSError, ValueError, sqlite3.Error) as exc:
        if args.json and iteration_id:
            try:
                result = inspect_iteration(args.root, args.proposal, iteration_id)
            except (OSError, ValueError, sqlite3.Error):
                result = {}
            if "schema_version" in result:
                code = ("stale_state_revision" if isinstance(exc, Invalid) and exc.field == "iteration.state_revision"
                        else "action_rejected")
                if result["status"] == "reconciling" and code != "stale_state_revision":
                    reason = "publication_not_verified"
                    result.update(status="blocked", blocker_reason=reason)
                    result["next_action"] = {"id": "resolve_blocker", "authorization": "blocked", "reason": reason,
                                             "arguments": {"iteration_id": iteration_id,
                                                           "state_revision": result["state_revision"]}}
                print(json.dumps(result | {"error": {"code": code, "message": str(exc)}},
                                 indent=2, ensure_ascii=False, allow_nan=False))
                return 1
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    elif args.diagnose:
        from .diagnose import format_report

        print(format_report(result["diagnosis"]))
    else:
        print(f"{result['iteration_id']}: {result['status']}")
    return 0

# Public spelling retained for callers that use the validation-oriented API.
validate_iteration_plan = parse_iteration_plan
load_iteration = inspect_iteration
