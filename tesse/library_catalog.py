"""Validate the declarative library inventory without importing any adapter."""

import re
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

LIBRARY_STATUSES = frozenset({'adopt_now', 'assess', 'avoid', 'rejected'})
_NAME = re.compile(r'[a-z][a-z0-9_]*')
_SEGMENT = _NAME.pattern
_NUMBER = r'(?:0|[1-9][0-9]*)'
ADAPTER_KEY = re.compile(rf'{_SEGMENT}(?:\.{_SEGMENT})+@{_NUMBER}\.{_NUMBER}\.{_NUMBER}')
_REQUIRED = frozenset({
    'id', 'name', 'status', 'priority', 'categories', 'license', 'python',
    'capabilities', 'adapter_key', 'causal_review', 'provenance',
})
_OPTIONAL = frozenset({'target_status', 'commercial_review', 'notes', 'gates'})
ADOPTION_GATES = frozenset({
    'license_review', 'version_lock', 'adapter_tests', 'io_contract',
    'warmup_timing', 'causal_review', 'provenance_hash', 'health_check',
})
_UNVERIFIED_PYTHON = {'verify_locked_version', 'legacy_uncertain'}


@dataclass(frozen=True)
class LibraryEntry:
    id: str
    name: str
    status: str
    priority: int
    categories: tuple[str, ...]
    license: str
    python: str
    capabilities: tuple[str, ...]
    adapter_key: str | None
    causal_review: str
    provenance: Mapping[str, str]
    target_status: str | None = None
    commercial_review: str | None = None
    notes: str | None = None
    gates: Mapping[str, str] | None = None


def _text(value, location: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ValueError(f'{location}: esperado texto não vazio, sem espaços nas bordas')
    return value


def _fields(value, location: str, required, optional=frozenset()) -> None:
    if not isinstance(value, dict):
        raise ValueError(f'{location}: esperado objeto')
    for field in sorted(required - value.keys()):
        raise ValueError(f'{location}.{field}: campo obrigatório ausente')
    for field in value.keys() - required - optional:
        raise ValueError(f'{location}.{field}: campo desconhecido')


def _names(value, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{location}: esperada lista não vazia')
    for item in value:
        if not _NAME.fullmatch(_text(item, location)):
            raise ValueError(f'{location}: identificador inválido: {item}')
    if len(value) != len(set(value)):
        raise ValueError(f'{location}: identificadores duplicados')
    return tuple(value)


def parse_library_catalog(document: object) -> Mapping[str, LibraryEntry]:
    """Parse schema v1; an inventory record alone never authorizes execution."""
    _fields(document, 'catalog', {'version', 'updated_at', 'statuses', 'libraries'})
    if type(document['version']) is not int or document['version'] != 1:
        raise ValueError('catalog.version: versão não suportada')
    if type(document['updated_at']) is not date:
        updated = _text(document['updated_at'], 'catalog.updated_at')
        try:
            date.fromisoformat(updated)
        except ValueError as exc:
            raise ValueError('catalog.updated_at: esperada data ISO') from exc
    if set(_names(document['statuses'], 'catalog.statuses')) != LIBRARY_STATUSES:
        raise ValueError('catalog.statuses: vocabulário de status inválido')
    rows = document['libraries']
    if not isinstance(rows, list) or not rows:
        raise ValueError('catalog.libraries: esperada lista não vazia')
    entries = {}
    keys = set()
    for index, row in enumerate(rows):
        where = f'catalog.libraries[{index}]'
        _fields(row, where, _REQUIRED, _OPTIONAL)
        for field in ('name', 'license', 'python', 'id', 'causal_review', 'status'):
            _text(row[field], f'{where}.{field}')
        if not _NAME.fullmatch(row['id']) or row['id'] in entries:
            raise ValueError(f'{where}.id: identificador inválido ou duplicado')
        for field in ('status', 'target_status'):
            if field in row and _text(row[field], f'{where}.{field}') not in LIBRARY_STATUSES:
                raise ValueError(f'{where}.{field}: status desconhecido')
        if row['causal_review'] not in {'required', 'strict', 'passed'}:
            raise ValueError(f'{where}.causal_review: revisão desconhecida')
        if type(row['priority']) is not int or row['priority'] < 1:
            raise ValueError(f'{where}.priority: esperado inteiro positivo')
        python = row['python']
        if python not in _UNVERIFIED_PYTHON:
            try:
                SpecifierSet(python)
            except InvalidSpecifier as exc:
                raise ValueError(f'{where}.python: restrição Python inválida') from exc
        key = row['adapter_key']
        if key is not None:
            if not ADAPTER_KEY.fullmatch(_text(key, f'{where}.adapter_key')) or key in keys:
                raise ValueError(f'{where}.adapter_key: chave inválida ou duplicada')
            keys.add(key)
        provenance = row['provenance']
        _fields(provenance, f'{where}.provenance', {'source'}, {'resolved_version', 'sha256'})
        for field, value in provenance.items():
            _text(value, f'{where}.provenance.{field}')
        if 'sha256' in provenance and not re.fullmatch(r'[0-9a-f]{64}', provenance['sha256']):
            raise ValueError(f'{where}.provenance.sha256: hash inválido')
        adopted = row['status'] == 'adopt_now'
        gates = row.get('gates', {})
        _fields(gates, f'{where}.gates', ADOPTION_GATES if adopted else (), ADOPTION_GATES)
        for gate, evidence in gates.items():
            _text(evidence, f'{where}.gates.{gate}')
        if 'resolved_version' in provenance:
            try:
                Version(provenance['resolved_version'])
            except InvalidVersion as exc:
                raise ValueError(f'{where}.provenance.resolved_version: versão inválida') from exc
        if adopted:
            if key is None or python in _UNVERIFIED_PYTHON or row['causal_review'] != 'passed':
                raise ValueError(f'{where}: adoção exige adapter_key, python e revisão causal')
            for field in ('resolved_version', 'sha256'):
                if field not in provenance:
                    raise ValueError(f'{where}.provenance.{field}: necessário para adoção')
        for field in ('commercial_review', 'notes'):
            if field in row:
                _text(row[field], f'{where}.{field}')
        entries[row['id']] = LibraryEntry(**(row | {
            'gates': MappingProxyType(gates.copy()),
            'categories': _names(row['categories'], f'{where}.categories'),
            'capabilities': _names(row['capabilities'], f'{where}.capabilities'),
            'provenance': MappingProxyType(provenance.copy()),
        }))
    return MappingProxyType(entries)


def load_library_catalog(path: str | Path) -> Mapping[str, LibraryEntry]:
    return parse_library_catalog(yaml.safe_load(Path(path).read_text(encoding='utf-8')))


def library_catalog_path() -> Path:
    """Locate the canonical checkout/archive file or the installed wheel copy."""
    checkout = Path(__file__).resolve().parent.parent / 'catalog' / 'library.yml'
    return checkout if checkout.is_file() else (
        Path(sysconfig.get_path('data')) / 'share' / 'tesse' / 'catalog' / 'library.yml')
