"""Capability contracts; third-party implementations stay outside consumer YAML."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Protocol

from numpy import ndarray

from tesse.indicators.base import Observation
from tesse.library_catalog import ADAPTER_KEY

Scalar = str | int | float | bool | None
Parameters = Mapping[str, Scalar]
Row = Mapping[str, Scalar]


@dataclass(frozen=True)
class AdapterDescription:
    library_id: str
    adapter_key: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if not ADAPTER_KEY.fullmatch(self.adapter_key):
            raise ValueError('adapter_key inválida')
        if not self.library_id or not self.capabilities:
            raise ValueError('library_id e capabilities são obrigatórios')

    @property
    def adapter_version(self) -> str:
        return self.adapter_key.rsplit('@', 1)[1]


@dataclass(frozen=True)
class AdapterHealth:
    available: bool
    resolved_version: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.available and not self.reason:
            raise ValueError('reason é obrigatório quando indisponível')
        if self.available and not self.resolved_version:
            raise ValueError('resolved_version é obrigatório quando disponível')


@dataclass(frozen=True)
class AdapterProvenance:
    description: AdapterDescription
    resolved_version: str
    license: str
    source: str
    parameters: Parameters
    hashes: Mapping[str, str]

    def to_dict(self) -> dict:
        return {
            'library_id': self.description.library_id, 'adapter_key': self.description.adapter_key,
            'adapter_version': self.description.adapter_version, 'resolved_version': self.resolved_version,
            'license': self.license, 'source': self.source,
            'parameters': dict(self.parameters), 'hashes': dict(self.hashes),
        }


@dataclass(frozen=True)
class AdapterInvocation:
    operation: str
    provenance: AdapterProvenance
    input_hashes: Mapping[str, str]
    output_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        for field in ('input_hashes', 'output_hashes'):
            object.__setattr__(self, field, MappingProxyType(dict(getattr(self, field))))

    def to_dict(self) -> dict:
        return dict(operation=self.operation, provenance=self.provenance.to_dict(),
                    input_hashes=dict(self.input_hashes), output_hashes=dict(self.output_hashes))


class MinuteCandle(Protocol):
    @property
    def timestamp(self) -> datetime: ...


class PrimitiveInvocation(Protocol):
    @property
    def observations(self) -> Sequence[Observation]: ...

    @property
    def provenance(self) -> AdapterProvenance: ...


class Adapter(Protocol):
    def describe(self) -> AdapterDescription: ...
    def health_check(self) -> AdapterHealth: ...


class PrimitiveAdapter(Adapter, Protocol):
    def describe_component(self, component: str) -> Mapping[str, object]: ...
    def validate_params(self, component: str, params: Parameters) -> Parameters: ...
    def evaluate(self, component: str, candles: Sequence[MinuteCandle], params: Parameters,
                 *, as_of: datetime) -> Sequence[Observation]: ...
    def evaluate_with_provenance(self, component: str, candles: Sequence[MinuteCandle],
                                 params: Parameters, *, as_of: datetime) -> PrimitiveInvocation: ...


class AuditedAdapter(Adapter, Protocol):
    @property
    def invocations(self) -> tuple[AdapterInvocation, ...]: ...


class LearnerAdapter(AuditedAdapter, Protocol):
    def fit(self, features: ndarray, labels: ndarray, params: Parameters) -> object: ...
    def predict_proba(self, model: object, features: ndarray) -> ndarray: ...


class CalibrationAdapter(AuditedAdapter, Protocol):
    def calibrate(self, learner: LearnerAdapter, features: ndarray, labels: ndarray,
                  params: Parameters, *, gap: int) -> object: ...


class EvidenceAdapter(Adapter, Protocol):
    def summarize(self, observations: Sequence[Row], params: Parameters) -> Mapping[str, Scalar]: ...


class RiskAdapter(Adapter, Protocol):
    def measure(self, returns: Sequence[float], params: Parameters) -> Mapping[str, float]: ...


class BacktestAdapter(Adapter, Protocol):
    def replay(self, candles: Sequence[Row], signals: Sequence[Row],
               params: Parameters) -> Sequence[Row]: ...


@dataclass(frozen=True)
class ApprovedOrderIntent:
    """Execution-owned order data; the adapter receives no policy or transport."""

    idempotency_key: str
    asset: str
    direction: Literal['CALL', 'PUT']
    investment: Decimal
    environment: Literal['TEST']


@dataclass(frozen=True)
class BrokerExecution:
    order_id: str
    status: str
    filled_amount: Decimal

    def __post_init__(self) -> None:
        for field in ('order_id', 'status'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{field}: obrigatório no resultado reconciliado')
        amount = self.filled_amount
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError('filled_amount: esperado Decimal finito não negativo')


class BrokerAdapter(Adapter, Protocol):
    def encode_order(self, intent: ApprovedOrderIntent) -> Mapping[str, Scalar]: ...
    def decode_execution(self, response: Mapping[str, Scalar]) -> BrokerExecution: ...
