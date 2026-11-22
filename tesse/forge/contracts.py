"""Validation primitives and immutable graph definitions."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from re import fullmatch
from types import MappingProxyType
from typing import Any

DEFAULT_WORKSPACE_ROOT = Path("forge")


class Invalid(ValueError):
    def __init__(self, field, message):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def mapping(value, field):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise Invalid(field, "esperado mapeamento com chaves de texto")
    return value


def keys(value, field, required=(), optional=()):
    result = mapping(value, field)
    missing = set(required) - result.keys()
    unknown = result.keys() - set(required) - set(optional)
    if missing:
        key = sorted(missing)[0]
        raise Invalid(f"{field}.{key}", "campo obrigatório ausente")
    if unknown:
        key = sorted(unknown)[0]
        raise Invalid(f"{field}.{key}", "campo desconhecido")
    return result


def slug(value, field):
    if not isinstance(value, str) or not fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) or len(value) > 64:
        raise Invalid(field, "ID inválido: use até 64 caracteres em kebab-case")
    return value


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise Invalid(field, "esperado texto não vazio")
    return value


def number(value, field, *, low=0, high=None, integer=False):
    types = (int,) if integer else (int, float)
    finite = type(value) is int or (type(value) is float and isfinite(value))
    if type(value) not in types or not finite or value < low or (high is not None and value > high):
        spec = "inteiro" if integer else "número finito"
        limit = f"entre {low} e {high}" if high is not None else f">= {low}"
        raise Invalid(field, f"esperado {spec} {limit}")
    return value


@dataclass(frozen=True)
class Workspace:
    root: Path
    data_root: Path | None = None
    context: str | None = None

    def effective_data_root(self, override=None):
        if override is not None:
            return Path(override).resolve()
        if self.data_root is not None:
            return (self.root / self.data_root).resolve()
        return self.root.parent / "data"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def as_dict(self):
        return {key: getattr(self, key).isoformat().replace("+00:00", "Z")
                for key in ("start", "end")}


@dataclass(frozen=True)
class Node:
    id: str
    component: str
    params: Mapping[str, Any]
    inputs: Mapping[str, tuple[str, str]]

    def __post_init__(self):
        object.__setattr__(self, "params", MappingProxyType({
            key: tuple(value) if isinstance(value, list) else value for key, value in self.params.items()
        }))
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))

    def resolved_parameters(self):
        return {key: list(value) if isinstance(value, tuple) else value for key, value in self.params.items()}

    def as_dict(self):
        return {"component": self.component, "params": self.resolved_parameters(),
                "inputs": {key: ".".join(ref) for key, ref in self.inputs.items()}}


@dataclass(frozen=True)
class Lineage:
    source: str
    symbol: str
    timeframe: str = "M1"

    @property
    def key(self):
        return f"{self.symbol}:{self.timeframe}:{self.source}"

    def as_dict(self):
        return {"source": self.source, "symbol": self.symbol, "timeframe": self.timeframe}


@dataclass(frozen=True)
class Experiment:
    id: str
    snapshot: Mapping[str, str]
    windows: Mapping[str, Window]
    nodes: tuple[Node, ...]
    result: tuple[str, str]
    result_kind: str
    criteria: Mapping[str, float] | None = None
    managed_lineage: Lineage | None = None

    def __post_init__(self):
        object.__setattr__(self, "snapshot", MappingProxyType(dict(self.snapshot)))
        object.__setattr__(self, "windows", MappingProxyType(dict(self.windows)))
        if self.criteria is not None:
            object.__setattr__(self, "criteria", MappingProxyType(dict(self.criteria)))

    def as_dict(self):
        result = {"snapshot": dict(self.snapshot), "windows": {k: v.as_dict() for k, v in self.windows.items()},
                  "nodes": {node.id: node.as_dict() for node in self.nodes},
                  "result": {"kind": self.result_kind, "from": ".".join(self.result)}}
        if self.criteria is not None:
            result["criteria"] = dict(self.criteria)
        if self.managed_lineage is not None:
            result["managed"] = {"schema_version": 1, "lineage": self.managed_lineage.as_dict()}
        return result


@dataclass(frozen=True)
class Proposal:
    id: str
    strategy: str
    question: str
    hypothesis: str
    experiments: Mapping[str, Experiment]
    knowledge: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "experiments", MappingProxyType(dict(self.experiments)))

    def as_dict(self):
        result = {"id": self.id, "strategy": self.strategy, "question": self.question,
                  "hypothesis": self.hypothesis}
        if self.knowledge:
            result["knowledge"] = list(self.knowledge)
        return result
