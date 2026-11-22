"""Resolver for versioned declarative catalog metadata."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .bindings import resolve_bindings
from .identity import validate_model_id, validate_system_id, validate_version
from .qualification import QualificationPolicy


class _CatalogLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        values = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.YAMLError("catalog YAML keys must be strings")
            if key in values:
                raise yaml.YAMLError(f"duplicate catalog YAML key: {key}")
            values[key] = self.construct_object(value_node, deep=deep)
        return values


def _read_catalog(path: Path):
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_CatalogLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read catalog YAML {path}: {exc}") from exc


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    kind: str
    identity: str
    version: str
    status: str
    provenance: Mapping[str, Any]
    metadata: Mapping[str, Any]

    @property
    def system_id(self):
        return self.identity if self.kind == "system" else self.metadata.get("system_id")


class CatalogRegistry:
    def __init__(self, path: str | Path | None = None):
        self.path = self._default_path() if path is None else Path(path)
        self._records: dict[tuple[str, str, str], RegistryRecord] = {}
        self.reload()

    @staticmethod
    def _default_path() -> Path:
        package_root = Path(__file__).resolve().parents[2]
        candidates = (
            Path.cwd() / "catalog/strategies",
            package_root / "share/tesse/catalog/strategies",  # pip --target
            Path(sys.prefix) / "share/tesse/catalog/strategies",
            package_root / "catalog/strategies",  # source checkout, any cwd
        )
        return next((p for p in candidates if p.exists() or p.is_symlink()), candidates[0])

    @staticmethod
    def _require_path(path: Path, *, directory: bool) -> None:
        if path.is_symlink():
            raise ValueError(f"catalog rejects symlink: {path}")
        valid = path.is_dir() if directory else path.is_file()
        if not valid:
            raise ValueError(f"invalid catalog path: {path}")

    def _read_definition(self, identity: str, version: str) -> dict:
        system_dir = self.path / identity
        path = system_dir / f"{version}.yml"
        self._require_path(self.path.parent, directory=True)
        self._require_path(self.path, directory=True)
        self._require_path(system_dir, directory=True)
        self._require_path(path, directory=False)
        definition = _read_catalog(path)
        if not isinstance(definition, dict) or (
            definition.get("system_id"), definition.get("system_version")
        ) != (identity, version):
            raise ValueError(f"catalog identity differs from directory and filename: {path}")
        unknown = definition.keys() - {
            "system_id", "system_version", "name", "description", "status", "provenance",
            "family", "behavior", "timeframe", "role", "rules", "model", "evidence", "scope",
            "components", "qualification", "component_bindings",
        }
        if unknown:
            raise ValueError(f"unknown System definition fields in {path}: {sorted(unknown)}")
        return definition

    def resolve_bindings(self, system_id: str, system_version: str):
        record = self.get(system_id, validate_version(system_version), kind="system")
        definition = self._read_definition(record.identity, record.version)
        return resolve_bindings(definition.get("component_bindings", {}))

    def reload(self):
        self._require_path(self.path, directory=True)
        records = {}
        for system_dir in sorted(self.path.iterdir()):
            self._require_path(system_dir, directory=True)
            identity = validate_system_id(system_dir.name)
            for path in sorted(system_dir.iterdir()):
                self._require_path(path, directory=False)
                if path.suffix != ".yml":
                    raise ValueError(f"noncanonical catalog definition path: {path}")
                version = validate_version(path.stem)
                definition = self._read_definition(identity, version)
                resolve_bindings(definition.get("component_bindings", {}), f"{path}:component_bindings")
                status = definition.get("status", "candidate")
                provenance = definition.get("provenance", {})
                if not isinstance(status, str) or not isinstance(provenance, dict):
                    raise ValueError(f"invalid catalog status or provenance: {path}")
                metadata = {k: v for k, v in definition.items() if k not in {"status", "provenance"}}
                metadata["definition"] = f"strategies/{identity}/{version}.yml"
                key = ("system", identity, version)
                if key in records:
                    raise ValueError(f"duplicate catalog identity: {identity}@{version}")
                records[key] = RegistryRecord("system", identity, version, status, provenance, metadata)
        self._records = records
        return self

    def get(
        self, identity: str, version: str | None = None, *, kind: str | None = None
    ) -> RegistryRecord:
        if kind is None:
            # Prefer the exact system grammar; model IDs append an asset to a
            # valid system ID and therefore also contain hyphens.
            try:
                validate_system_id(identity)
            except ValueError:
                kind = "model"
            else:
                kind = "system"
        validate_model_id(identity) if kind == "model" else validate_system_id(identity)
        if version is not None:
            validate_version(version)
            try:
                return self._records[(kind, identity, version)]
            except KeyError as exc:
                raise KeyError(f"unknown {kind}: {identity}@{version}") from exc
        matches = [r for (k, i, _), r in self._records.items() if k == kind and i == identity]
        if not matches:
            raise KeyError(f"unknown {kind}: {identity}")
        return max(matches, key=lambda r: _version_key(r.version))

    def resolve_qualification(
        self, system_id: str, system_version: str, *, asset: str, environment: str, purpose: str
    ) -> QualificationPolicy:
        """Read and verify the exact registered definition; never fall back to another version."""
        record = self.get(system_id, validate_version(system_version), kind="system")
        definition = self._read_definition(record.identity, record.version)
        policy = QualificationPolicy.from_definition(
            record.identity, record.version, definition.get("qualification")
        )
        if not policy.scope.authorizes(asset=asset, environment=environment, purpose=purpose):
            raise ValueError("qualification scope does not authorize this asset/environment/purpose")
        return policy

    def records(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[RegistryRecord]:
        return sorted(
            (
                r
                for r in self._records.values()
                if (kind is None or r.kind == kind) and (status is None or r.status == status)
            ),
            key=lambda r: (r.kind, r.identity, _version_key(r.version)),
        )
