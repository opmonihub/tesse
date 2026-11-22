"""Validate translation only; the Tesse executor retains submission and reconciliation."""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

from .contracts import ApprovedOrderIntent, BrokerAdapter, BrokerExecution, Scalar


def encode_approved_order(adapter: BrokerAdapter,
                          intent: ApprovedOrderIntent) -> Mapping[str, Scalar]:
    """An adapter may serialize the approved order but may not amend it."""
    if not isinstance(intent, ApprovedOrderIntent):
        raise ValueError('ordem exige intenção aprovada pelo Tesse')
    if intent.environment != 'TEST':
        raise ValueError('environment: somente TEST')
    if not intent.idempotency_key or not intent.asset or intent.direction not in {'CALL', 'PUT'}:
        raise ValueError('intenção incompleta')
    if (not isinstance(intent.investment, Decimal) or not intent.investment.is_finite()
            or intent.investment <= 0):
        raise ValueError('investment: valor inválido')
    expected = {
        'asset': intent.asset, 'direction': intent.direction,
        'investment': str(intent.investment), 'environment': intent.environment,
        'idempotency_key': intent.idempotency_key,
    }
    payload = dict(adapter.encode_order(intent))
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ValueError(f'{field}: tradução alterou intenção aprovada')
    extra = payload.keys() - expected.keys()
    if extra:
        raise ValueError(f'campos não autorizados: {", ".join(sorted(extra))}')
    return MappingProxyType(expected)


def decode_reconciled_execution(adapter: BrokerAdapter, response: Mapping[str, Scalar], *,
                                reconciled: BrokerExecution) -> BrokerExecution:
    """An adapter interpretation cannot override the Tesse-owned reconciled fill."""
    if not isinstance(reconciled, BrokerExecution):
        raise ValueError('resultado exige referência da reconciliação do Tesse')
    result = adapter.decode_execution(response)
    if not isinstance(result, BrokerExecution):
        raise ValueError('adapter não retornou um BrokerExecution')
    for field in ('order_id', 'status', 'filled_amount'):
        if getattr(result, field) != getattr(reconciled, field):
            raise ValueError(f'{field}: adapter diverge da reconciliação do Tesse')
    return reconciled
