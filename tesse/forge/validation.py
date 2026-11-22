"""Read-only validation for both conversation proposals and CLI execution."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from re import fullmatch
from types import MappingProxyType

import yaml

from .contracts import (
    Experiment,
    Invalid,
    Lineage,
    Proposal,
    Window,
    Workspace,
    keys,
    mapping,
    number,
    slug,
    text,
)
from .graph import compile_graph, validate_managed_graph
from .layout import project_directory

MAX_YAML = 256 * 1024
SCHEMA_VERSION = 2


class DefinitionLoader(yaml.SafeLoader):
    # Preserve UTC strings in the effective inputs instead of implicit YAML dates.
    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases YAML não são permitidos")
        if self.depth >= 32:
            raise yaml.YAMLError("YAML excede o limite de aninhamento")
        self.depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        if not isinstance(node, yaml.MappingNode):
            raise yaml.YAMLError("esperado mapeamento")
        for raw_key, raw_value in node.value:
            key = self.construct_object(raw_key, deep=deep)
            if not isinstance(key, str):
                raise yaml.YAMLError("chaves YAML precisam ser texto")
            if key in result:
                raise yaml.YAMLError(f"chave duplicada: {key}")
            result[key] = self.construct_object(raw_value, deep=deep)
        return result


@dataclass(frozen=True)
class Issue:
    level: str
    path: str
    message: str


@dataclass(frozen=True)
class Validation:
    definition: Proposal | None
    issues: tuple[Issue, ...] = ()
    hashes: Mapping[str, str] | None = None
    workspace: Workspace | None = None

    def __post_init__(self):
        if self.hashes is not None:
            object.__setattr__(self, "hashes", MappingProxyType(dict(self.hashes)))

    def valid(self, *, strict=False):
        return self.definition is not None and not (strict and self.issues)


def read_limited(path: Path):
    with path.open("rb") as stream:
        return stream.read(MAX_YAML + 1)


def parse_yaml(data, filename, *, limit=MAX_YAML):
    if limit is not None and len(data) > limit:
        raise Invalid(filename, "YAML excede 256 KiB")
    try:
        parsed = yaml.load(data, Loader=DefinitionLoader)
    except (yaml.YAMLError, UnicodeError, ValueError, OverflowError) as exc:
        raise Invalid(filename, f"YAML inválido: {exc}") from exc
    return mapping(parsed, filename)


@lru_cache(maxsize=1)
def artifact_paths():
    filename = "artifacts.yaml"
    manifest = parse_yaml(files("tesse.forge").joinpath(filename).read_bytes(), filename)
    artifacts = manifest["artifacts"]
    expected = [("proposal", []), ("protocol", ["proposal"]), ("run", ["proposal", "protocol"])]
    if manifest["version"] != SCHEMA_VERSION or [(a["id"], a["requires"]) for a in artifacts] != expected:
        raise Invalid(filename, "contrato empacotado incompatível")
    paths = {a["id"]: a["generates"] for a in artifacts}
    if paths != {"proposal": "proposal.yaml", "protocol": "protocol.yaml", "run": "runs/*/run.yaml"}:
        raise Invalid(filename, "caminhos de artefatos incompatíveis")
    return MappingProxyType(paths)


def parse_utc(value, field):
    if not isinstance(value, str) or not fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:Z|\+00:00)", value):
        raise Invalid(field, "esperado timestamp UTC alinhado a M1 (AAAA-MM-DDTHH:MM:00Z)")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as exc:
        raise Invalid(field, "data UTC inválida") from exc


def parse_windows(value, field):
    names = ("train", "calibration", "evaluation")
    value = keys(value, field, required=names)
    windows = {}
    for name in names:
        prefix = f"{field}.{name}"
        bounds = keys(value[name], prefix, required=("start", "end"))
        start, end = (parse_utc(bounds[k], f"{prefix}.{k}") for k in ("start", "end"))
        if start >= end:
            raise Invalid(prefix, "start precisa ser anterior a end")
        if windows and start < next(reversed(windows.values())).end:
            raise Invalid(prefix, "janelas fora de ordem ou com sobreposição")
        windows[name] = Window(start, end)
    return windows


def _snapshot(value, field):
    value = keys(value, field, required=("manifest", "manifest_sha256", "sha256"))
    name = text(value["manifest"], f"{field}.manifest")
    path = PurePosixPath(name)
    if (path.is_absolute() or path.suffix != ".json" or "\\" in name or "\x00" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise Invalid(f"{field}.manifest", "caminho relativo normalizado para manifesto JSON")
    for key in ("sha256", "manifest_sha256"):
        if not isinstance(value[key], str) or not fullmatch(r"[a-f0-9]{64}", value[key]):
            raise Invalid(f"{field}.{key}", "esperado SHA-256 hexadecimal minúsculo")
    return value


def versioned_yaml(data, filename, *, limit=MAX_YAML):
    result = parse_yaml(data, filename, limit=limit)
    require_version(result, filename, SCHEMA_VERSION)
    return result


def require_version(document, field, expected):
    version = document.get("schema_version")
    if type(version) is not int or version != expected:
        raise Invalid(f"{field}.schema_version", f"versão esperada: {expected}")


def parse_lineage(value, field):
    value = keys(value, field, required=("source", "symbol", "timeframe"))
    source = text(value["source"], f"{field}.source")
    symbol = value["symbol"]
    if (source != source.strip() or len(source) > 256
            or any(not character.isprintable() for character in source)):
        raise Invalid(f"{field}.source", "esperada fonte canônica literal do manifesto")
    if not isinstance(symbol, str) or not fullmatch(r"[A-Z0-9]{1,32}", symbol):
        raise Invalid(f"{field}.symbol", "símbolo inválido")
    if value["timeframe"] != "M1":
        raise Invalid(f"{field}.timeframe", "série esperada: M1")
    return Lineage(source, symbol)


def load_workspace(root):
    root = Path(root).resolve()
    config = root / "config.yaml"
    if config.is_symlink() or not config.is_file():
        raise Invalid(config.name, f"configuração obrigatória ausente ou não regular em {root}")
    try:
        raw = read_limited(config)
    except OSError as exc:
        raise Invalid(config.name, f"não foi possível ler {config}: {exc}") from exc
    data = keys(versioned_yaml(raw, config.name), config.name, required=("schema_version",),
                optional=("context", "data_root"))
    context = text(data["context"], "config.yaml.context") if "context" in data else None
    data_root = text(data["data_root"], "config.yaml.data_root") if "data_root" in data else None
    if data_root is not None and "\x00" in data_root:
        raise Invalid("config.yaml.data_root", "caminho inválido")
    return Workspace(root, Path(data_root) if data_root is not None else None, context)


def knowledge_paths(workspace, references, field="proposal.yaml.knowledge"):
    if not isinstance(references, list):
        raise Invalid(field, "esperada lista de caminhos Markdown")
    paths = {}
    for i, value in enumerate(references):
        location = f"{field}.{i}"
        name = text(value, location)
        parts = name.split("/")
        if (len(parts) < 2 or parts[0] != "knowledge" or "\\" in name or "\x00" in name
                or any(part in ("", ".", "..") for part in parts) or PurePosixPath(name).suffix != ".md"):
            raise Invalid(location, "esperado caminho relativo knowledge/<conceito>.md")
        if workspace is None:
            raise Invalid(location, "a referência exige um workspace Forge")
        directory = workspace.root / "knowledge"
        try:
            path = (workspace.root / name).resolve()
            if (directory.is_symlink() or not path.is_relative_to(directory)
                    or path.suffix != ".md" or not path.is_file()):
                raise Invalid(location, "nota ausente ou fora do diretório knowledge do workspace")
        except OSError as exc:
            raise Invalid(location, f"não foi possível resolver a nota: {exc}") from exc
        paths[name] = path
    return paths


def _criteria(value, field):
    bounds = {"min_winrate": 1, "min_trades_per_day": 1440, "min_improvement_pp": 100}
    value = keys(value, field, required=bounds)
    return {key: number(value[key], f"{field}.{key}", high=limit) for key, limit in bounds.items()}


def validate_documents(proposal_data, protocol_data, *, expected_id=None, workspace=None):
    paths = artifact_paths()
    pf, ef = paths["proposal"], paths["protocol"]
    fields = ("strategy", "question", "hypothesis")
    definition = keys(versioned_yaml(proposal_data, pf), pf, required=("schema_version", "id", *fields),
                      optional=("knowledge",))
    proposal_id = slug(definition["id"], f"{pf}.id")
    if expected_id is not None and proposal_id != expected_id:
        raise Invalid(f"{pf}.id", "ID diferente do diretório da proposta")
    statements = {key: text(definition[key], f"{pf}.{key}") for key in fields}
    knowledge = tuple(knowledge_paths(workspace, definition.get("knowledge", [])))

    protocol = keys(versioned_yaml(protocol_data, ef), ef, required=("schema_version", "proposal", "experiments"))
    if protocol["proposal"] != proposal_id:
        raise Invalid(f"{ef}.proposal", "não corresponde ao ID de proposal.yaml")
    raw = mapping(protocol["experiments"], f"{ef}.experiments")
    if not 1 <= len(raw) <= 64:
        raise Invalid(f"{ef}.experiments", "esperados de 1 a 64 experimentos")
    experiments, issues = {}, []
    for e_id, value in raw.items():
        field = f"{ef}.experiments.{e_id}"
        slug(e_id, field)
        value = keys(value, field, required=("snapshot", "windows", "nodes", "result"), optional=("criteria", "managed"))
        result_field = f"{field}.result"
        binding = keys(value["result"], result_field, required=("kind", "from"))
        kind = text(binding["kind"], f"{result_field}.kind")
        port_type = {"comparison": "Comparison", "predictions": "ScoredEvaluation"}.get(kind)
        if port_type is None:
            raise Invalid(f"{result_field}.kind", "use comparison ou predictions")
        criteria_path = f"{field}.criteria"
        criteria = None
        if kind == "comparison":
            if "criteria" not in value:
                raise Invalid(criteria_path, "comparison exige critérios próprios")
            criteria = _criteria(value["criteria"], criteria_path)
            if not criteria["min_improvement_pp"]:
                issues.append(Issue("WARNING", f"{criteria_path}.min_improvement_pp", "o critério não exige melhora estrita"))
        elif "criteria" in value:
            raise Invalid(criteria_path, "predictions não aceita critérios de comparação")
        snapshot = _snapshot(value["snapshot"], f"{field}.snapshot")
        windows = parse_windows(value["windows"], f"{field}.windows")
        nodes, result = compile_graph(value["nodes"], binding["from"], f"{field}.nodes", port_type)
        lineage = None
        if "managed" in value:
            managed_field = f"{field}.managed"
            managed = keys(value["managed"], managed_field, required=("schema_version", "lineage"))
            require_version(managed, managed_field, 1)
            lineage = parse_lineage(managed["lineage"], f"{managed_field}.lineage")
            if kind != "comparison":
                raise Invalid(managed_field, "modo gerenciado exige comparison")
            validate_managed_graph(nodes, result, managed_field)
        experiments[e_id] = Experiment(e_id, snapshot, windows, nodes, result, kind, criteria, lineage)
    return Validation(Proposal(proposal_id, **statements, experiments=experiments, knowledge=knowledge), tuple(issues),
                      {pf: hashlib.sha256(proposal_data).hexdigest(), ef: hashlib.sha256(protocol_data).hexdigest()}, workspace)


def validate_proposal(root, proposal_id):
    try:
        workspace = root if isinstance(root, Workspace) else load_workspace(root)
        path = project_directory(workspace.root, proposal_id)
        if not path.is_dir():
            raise Invalid("proposal", "proposta ativa não encontrada")
        documents = []
        for kind in ("proposal", "protocol"):
            filename = artifact_paths()[kind]
            target = path / filename
            if target.is_symlink() or not target.is_file():
                raise Invalid(filename, "arquivo obrigatório ausente ou não regular")
            try:
                documents.append(read_limited(target))
            except OSError as exc:
                raise Invalid(filename, f"não foi possível ler: {exc}") from exc
        return validate_documents(*documents, expected_id=proposal_id, workspace=workspace)
    except Invalid as exc:
        return Validation(None, (Issue("ERROR", exc.field, exc.message),))
