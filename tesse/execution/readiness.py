"""Deterministic operational readiness drill for the TEST worker.

The drill is deliberately read-only: it evaluates evidence supplied by the
worker/SDK and never changes configuration, Store state, or broker environment.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_REQUIRED_OBSERVE = (
    "environment",
    "provider",
    "model",
    "candles",
    "broker_balance",
    "signal_identity",
    "snapshot",
)
_REQUIRED_ORDER = _REQUIRED_OBSERVE + ("settlement_recovery", "order_test")
def _terminal_evidence(order_id, status, recovery) -> bool:
    return (
        isinstance(order_id, str) and bool(order_id.strip()) and recovery is True
        and isinstance(status, str) and status in {"WIN", "LOSS", "LOSE", "TIE", "REFUNDED"}
    )


def _has_order_evidence(value: Mapping[str, Any]) -> bool:
    if _terminal_evidence(value.get("order_id"), value.get("settlement_status"), value.get("recovery_verified")):
        return True
    order = value.get("order") if isinstance(value.get("order"), Mapping) else {}
    settlement = value.get("settlement") if isinstance(value.get("settlement"), Mapping) else {}
    recovery = value.get("recovery") if isinstance(value.get("recovery"), Mapping) else {}
    return _terminal_evidence(order.get("id"), settlement.get("status"), recovery.get("verified"))


EVIDENCE_MAX_AGE = timedelta(hours=2)
_POINTER_FIELDS = ("asset", "model_id", "model_version", "card_hash", "weights_hash", "provider", "commit_hash")


def _risk_fingerprint(config: Any) -> str:
    from ..config import effective_operation

    operation = effective_operation(config) if hasattr(config, "operation") else config
    payload = operation.model_dump() if hasattr(operation, "model_dump") else dict(operation)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def evidence_context(config, account_binding, pointer, snapshot_hash, at, *, evidence_window=None):
    if not account_binding or not pointer or not snapshot_hash or at.utcoffset() is None:
        return None
    if any(not pointer.get(key) for key in _POINTER_FIELDS):
        return None
    session = config.operation.session
    return {
        "risk_fingerprint": _risk_fingerprint(config), "config_fingerprint": config.fingerprint(),
        "account_binding": account_binding, "environment": config.runtime.environment,
        "pointer": {key: pointer[key] for key in _POINTER_FIELDS}, "snapshot_hash": snapshot_hash,
        "window": {"day": at.astimezone(ZoneInfo(session.timezone)).date().isoformat(),
                   "timezone": session.timezone, "start": session.start, "end": session.end,
                   "data": evidence_window},
    }


def store_evidence_context(config, store, account_binding, asset, at):
    from ..runtime.active_snapshot import active_snapshot_hash

    with store.transaction():
        pointer = store.active_pointer(asset)
        # The Store is the authority; a stale file cannot supply the context.
        digest = active_snapshot_hash(store.active_pointers())
        window = None
        if pointer:
            row = store.db.execute(
                "SELECT decision_day FROM routing_decisions WHERE asset=? AND selected_model_id=? "
                "AND decision_day<=? ORDER BY decision_day DESC LIMIT 1", (asset, pointer["model_id"], at.date().isoformat()),
            ).fetchone()
            if row:
                decision = store.routing_decision(asset, row[0])
                if decision and decision.get("decision_sha256"):
                    window = decision["evidence_window"]
    return evidence_context(config, account_binding, pointer, digest, at, evidence_window=window)


def _context_blocker(evidence, context, now):
    if not isinstance(context, Mapping) or context.get("environment") != "TEST" or evidence.get("context") != context:
        return "evidence_context"
    try:
        verified_at = datetime.fromisoformat(evidence["verified_at"])
        if verified_at.utcoffset() is None or now.utcoffset() is None:
            return "evidence_validity"
    except (KeyError, TypeError, ValueError):
        return "evidence_validity"
    if not timedelta(0) <= now - verified_at <= EVIDENCE_MAX_AGE:
        return "evidence_validity"
    return None


def is_test_environment(config: Any) -> bool:
    return (
        getattr(getattr(config, "operation", None), "environment", None) == "TEST"
        and getattr(getattr(config, "runtime", None), "environment", None) == "TEST"
    )


def live_readiness_report(
    config: Any,
    *,
    provider_healthy: bool,
    model_active: bool,
    candles: bool,
    broker_balance: bool,
    signal_identity: bool,
    snapshot_fresh: bool,
    sdk_ready: bool,
    observe_only: bool,
) -> dict[str, Any]:
    """Evaluate current TEST checks; prior drills are not a source of permission."""
    checks = {
        "environment": is_test_environment(config),
        "provider": provider_healthy,
        "model": model_active,
        "candles": candles,
        "broker_balance": broker_balance,
        "signal_identity": signal_identity,
        "snapshot": snapshot_fresh,
        "sdk_orders_ready": sdk_ready,
    }
    required = _REQUIRED_OBSERVE if observe_only else (*_REQUIRED_OBSERVE, "sdk_orders_ready")
    blocked = [name for name in required if not checks[name]]
    return {
        "state": "blocked" if blocked else "observe_only_ready" if observe_only else "test_order_ready",
        "environment": config.operation.environment,
        "checks": checks,
        "blocked_by": blocked,
    }


def readiness_report(
    config: Any,
    *,
    provider_healthy: bool = False,
    model_active: bool = False,
    candles: bool = False,
    broker_balance: bool = False,
    signal_identity: bool = False,
    snapshot_fresh: bool = False,
    settlement_recovered: bool = False,
    test_order: bool = False,
    evidence: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate evidence for an optional drill; not used to authorize live sends."""
    operation_env = getattr(getattr(config, "operation", None), "environment", None)
    checks = {
        "environment": is_test_environment(config),
        "provider": bool(provider_healthy),
        "model": bool(model_active),
        "candles": bool(candles),
        "broker_balance": bool(broker_balance),
        "signal_identity": bool(signal_identity),
        "snapshot": bool(snapshot_fresh),
        "settlement_recovery": bool(settlement_recovered),
        "order_test": bool(test_order),
    }
    evidence_data = dict(evidence or {})
    # Accept the structured drill receipt as the canonical source while
    # preserving flat Store flags for older callers. This keeps the worker and
    # CLI from requiring two representations of the same completed drill.
    order = evidence_data.get("order") if isinstance(evidence_data.get("order"), Mapping) else {}
    settlement = evidence_data.get("settlement") if isinstance(evidence_data.get("settlement"), Mapping) else {}
    recovery = evidence_data.get("recovery") if isinstance(evidence_data.get("recovery"), Mapping) else {}
    settlement_recovered = bool(settlement_recovered or (settlement.get("status") and recovery.get("verified")))
    test_order = bool(test_order or order.get("id"))
    checks["settlement_recovery"] = settlement_recovered
    checks["order_test"] = test_order
    order_evidence_ok = _has_order_evidence(evidence_data)
    binding_error = _context_blocker(evidence_data, context, now or datetime.now(UTC))
    if context and (context.get("risk_fingerprint") != _risk_fingerprint(config)
                    or context.get("config_fingerprint") != config.fingerprint()):
        binding_error = "evidence_context"
    blocked_by = [name for name in _REQUIRED_OBSERVE if not checks[name]]
    if not blocked_by and not checks["settlement_recovery"]:
        # Recovery evidence is a prerequisite for controlled order submission,
        # while observation remains safe without it.
        blocked_by.append("settlement_recovery")
    if not blocked_by and not checks["order_test"]:
        blocked_by.append("order_test")
    if not blocked_by and checks["order_test"] and not order_evidence_ok:
        blocked_by.append("order_evidence")
    if not blocked_by and binding_error:
        blocked_by.append(binding_error)

    if not all(checks[name] for name in _REQUIRED_OBSERVE):
        state = "blocked"
    elif all(checks[name] for name in _REQUIRED_ORDER) and order_evidence_ok and not binding_error:
        state = "test_order_ready"
    else:
        state = "observe_only_ready"

    return {
        "state": state,
        "environment": operation_env,
        "checks": checks,
        "blocked_by": blocked_by,
        "risk_fingerprint": _risk_fingerprint(config),
        "evidence": evidence_data,
        "context": context,
    }


def order_submission_allowed(report: Mapping[str, Any] | None, *, context=None, now=None) -> bool:
    """Check drill evidence for audit; the ordinary worker does not consume it."""
    if not isinstance(report, Mapping):
        return False
    evidence, checks = report.get("evidence"), report.get("checks")
    return (
        report.get("state") == "test_order_ready" and report.get("environment") == "TEST"
        and report.get("context") == context and isinstance(evidence, Mapping)
        and isinstance(checks, Mapping) and all(checks.get(key) is True for key in _REQUIRED_ORDER)
        and _has_order_evidence(evidence)
        and _context_blocker(evidence, context, now or datetime.now(UTC)) is None
    )
