"""Exact adapter lookup; declarative values never become import paths."""

import platform
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType

from packaging.specifiers import SpecifierSet

from tesse.library_catalog import (
    ADAPTER_KEY,
    LibraryEntry,
    library_catalog_path,
    load_library_catalog,
)

from .contracts import Adapter


@dataclass(frozen=True)
class Registration:
    key: str
    library_id: str
    factory: Callable[[], Adapter]


@dataclass(frozen=True)
class Resolution:
    key: str
    adapter: Adapter | None = None
    library: LibraryEntry | None = None
    resolved_version: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.adapter is not None


class AdapterRegistry:
    def __init__(self, catalog: Mapping[str, LibraryEntry],
                 registrations: Iterable[Registration] = ()) -> None:
        self._catalog = MappingProxyType(dict(catalog))
        factories = {}
        for item in registrations:
            if not ADAPTER_KEY.fullmatch(item.key):
                raise ValueError(f'adapter_key inválida: {item.key}')
            if item.key in factories:
                raise ValueError(f'adapter_key duplicada: {item.key}')
            if item.library_id not in self._catalog:
                raise ValueError(f'biblioteca não registrada: {item.library_id}')
            factories[item.key] = item
        self._factories = MappingProxyType(factories)

    def statuses(self) -> dict[str, str]:
        """Inventory and registered capability status without loading factories."""
        statuses = {entry.adapter_key: entry.status for entry in self._catalog.values() if entry.adapter_key}
        statuses.update({key: self._catalog[item.library_id].status for key, item in self._factories.items()})
        return dict(sorted(statuses.items()))

    def resolve(self, key: str) -> Resolution:
        if not isinstance(key, str) or not ADAPTER_KEY.fullmatch(key):
            return Resolution(key, reason='adapter_key inválida; use chave versionada')
        registration = self._factories.get(key)
        if registration is None:
            library = next((entry for entry in self._catalog.values() if entry.adapter_key == key), None)
            if library and library.status != 'adopt_now':
                return Resolution(key, library=library, reason=f'biblioteca com status {library.status}')
            return Resolution(key, reason='adapter_key não registrada')
        library = self._catalog[registration.library_id]
        if library.status != 'adopt_now':
            return Resolution(key, library=library, reason=f'biblioteca com status {library.status}')
        if platform.python_version() not in SpecifierSet(library.python):
            return Resolution(key, library=library, reason=f'Python incompatível com {library.python}')
        try:
            adapter = registration.factory()
            description = adapter.describe()
            if description.adapter_key != key or description.library_id != library.id:
                return Resolution(key, library=library, reason='identidade do adapter divergente')
            health = adapter.health_check()
        except Exception as exc:
            return Resolution(key, library=library, reason=f'falha de health: {type(exc).__name__}')
        if not health.available:
            return Resolution(key, library=library, reason=health.reason)
        locked = library.provenance.get('resolved_version')
        if not locked or locked != health.resolved_version:
            return Resolution(key, library=library,
                              reason=f'versão instalada {health.resolved_version} difere do lock {locked}')
        return Resolution(key, adapter, library, health.resolved_version)


def _talib() -> Adapter:
    from .talib_adapter import REGISTRATION

    return REGISTRATION.factory()


_SKLEARN_KEYS = (
    'learners.sklearn.hist_gradient_boosting@1.0.0',
    'selection.sklearn.time_series_split@1.0.0',
    'calibration.sklearn.sigmoid@1.0.0',
)


def _sklearn(key: str) -> Adapter:
    from .sklearn_adapter import REGISTRATIONS

    return next(entry.factory() for entry in REGISTRATIONS if entry.key == key)


def _xgboost() -> Adapter:
    from .xgboost_adapter import XGBoostAdapter

    return XGBoostAdapter()


def default_registry() -> AdapterRegistry:
    """Gate reviewed library metadata before invoking any compiled-in factory."""
    try:
        catalog = load_library_catalog(library_catalog_path())
    except (OSError, ValueError) as exc:
        raise ValueError(f'catálogo de bibliotecas indisponível: {exc}') from exc
    registrations = (Registration('library.talib@1.0.0', 'talib', _talib),
                     Registration('learners.xgboost.gradient_boosting@1.0.0', 'xgboost', _xgboost),
                     *(Registration(key, 'sklearn', partial(_sklearn, key)) for key in _SKLEARN_KEYS))
    return AdapterRegistry(catalog, registrations)
