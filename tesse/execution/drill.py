"""Bounded, auditable one-order TEST drill orchestration.

The drill is deliberately separate from ``start``: it can consume exactly one
operator authorization through :class:`~tesse.execution.executor.Executor`, but
it never fabricates readiness and never re-submits a pending intent on resume.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from ebinex_sdk.exceptions import TransportError

from ..core import utcnow
from ..models.assets import canonicalize_asset
from ..runtime.active_snapshot import active_snapshot_hash
from ..state import Store
from .readiness import readiness_report, store_evidence_context


@dataclass(frozen=True, slots=True)
class DrillPlan:
    id: str
    asset: str
    model_id: str
    model_version: str
    fingerprint: str
    expires_at: str
    context: dict[str, Any]
    status: str = "PLANNED"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def plan_test_drill(
    store: Store,
    config: Any,
    *,
    asset: str,
    account_binding: str,
    actor: str,
    now: datetime | None = None,
    ttl: timedelta = timedelta(minutes=15),
) -> dict[str, Any]:
    """Create a no-side-effect plan tied to the current pointer and context."""
    now = now or utcnow()
    if now.utcoffset() is None:
        raise ValueError("drill clock must be timezone-aware")
    if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
        raise ValueError("drill expiry must be between one second and 15 minutes")
    pointer = store.active_pointer(asset)
    if not pointer:
        raise ValueError("test drill requires an active model pointer")
    metadata = store.get_model_metadata(pointer["model_id"], pointer["model_version"])
    if not metadata or metadata["status"] != "active":
        raise ValueError("test drill requires active model metadata")
    context = store_evidence_context(config, store, account_binding, asset, now)
    if context is None:
        raise ValueError("test drill context is unavailable")
    context = {**context, "drill": {"mode": "TEST", "asset": asset, "timeframe": "M1", "order_mode": "OPTION"}}
    fingerprint = _fingerprint({"asset": asset, "pointer": pointer, "context": context, "actor": actor})
    expires = now + ttl
    return store.create_test_drill(
        asset=asset,
        model_id=pointer["model_id"],
        model_version=pointer["model_version"],
        card_hash=pointer["card_hash"],
        weights_hash=pointer["weights_hash"],
        provider=pointer["provider"],
        account_binding=account_binding,
        context=context,
        fingerprint=fingerprint,
        actor=actor,
        created_at=now,
        expires_at=expires,
    )


def authorize_test_drill(store: Store, drill_id: str, *, fingerprint: str, actor: str, now=None):
    return store.authorize_test_drill(drill_id, fingerprint=fingerprint, actor=actor, now=now or utcnow())


def record_recovery(
    store: Store,
    *,
    drill_id: str,
    intent_id: str,
    order_id: str,
    client_order_id: str | None,
    settlement_status: str,
    source: str,
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist broker-backed recovery without exposing journal secrets."""
    if not all(isinstance(value, str) and value for value in (drill_id, intent_id, order_id, settlement_status, source)):
        raise ValueError("recovery identity is incomplete")
    if not isinstance(verified_at or utcnow(), datetime) or (verified_at or utcnow()).utcoffset() is None:
        raise ValueError("recovery timestamp must be timezone-aware")
    body = {
        "drill_id": drill_id,
        "intent_id": intent_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "settlement_status": settlement_status,
        "verified": True,
        "source": source,
        "verified_at": (verified_at or utcnow()).isoformat(),
    }
    store.set(f"test_recovery:{drill_id}", body)
    store.register_recovery("test_drill_order", body, resolved=True)
    return body


def publish_test_receipt(
    store: Store,
    config: Any,
    *,
    drill_id: str,
    recovery: Mapping[str, Any],
    now: datetime | None = None,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive an idempotent two-hour receipt from settled Store + recovery."""
    now = now or utcnow()
    drill = store.test_drill(drill_id)
    if drill is None:
        raise ValueError("unknown test drill")
    if drill["status"] not in {"SETTLED", "RECEIPTED"} or not drill.get("intent_id"):
        raise ValueError("test drill is not settled")
    if recovery.get("verified") is not True or not recovery.get("order_id"):
        raise ValueError("broker-backed recovery is required")
    try:
        verified_at = datetime.fromisoformat(recovery.get("verified_at", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("recovery timestamp is invalid") from exc
    if verified_at.utcoffset() is None or verified_at > now:
        raise ValueError("recovery timestamp is in the future")
    intent = store.db.execute("SELECT * FROM intents WHERE id=?", (drill["intent_id"],)).fetchone()
    if intent is None or intent["state"] not in {"WIN", "LOSS", "TIE", "REFUNDED"}:
        raise ValueError("terminal intent is not corroborated")
    if recovery.get("settlement_status") != intent["state"]:
        raise ValueError("recovery settlement differs from Store settlement")
    context = store_evidence_context(config, store, drill["account_binding"], drill["asset"], now)
    if context is None or context != drill["context"]:
        # The drill stores a richer context (including mode/timeframe). Compare
        # the canonical evidence portion while preserving the explicit drill
        # declaration in the receipt.
        if not context or any(drill["context"].get(key) != context.get(key) for key in ("risk_fingerprint", "config_fingerprint", "account_binding", "environment", "pointer", "snapshot_hash", "window")):
            raise ValueError("test drill context no longer matches")
    evidence = {
        "drill_id": drill_id,
        "intent_id": drill["intent_id"],
        "context": context,
        "order": {"id": recovery["order_id"]},
        "settlement": {"status": recovery["settlement_status"]},
        "recovery": {key: recovery[key] for key in ("verified", "source", "verified_at") if key in recovery},
        "verified_at": recovery.get("verified_at", now.isoformat()),
    }
    checks = dict((observation or {}).get("checks") or (store.get("readiness") or {}).get("checks") or {})
    # The receipt itself supplies only order/recovery evidence. Existing fresh
    # observation checks must still be present; no boolean is invented here.
    report = readiness_report(
        config,
        provider_healthy=bool(checks.get("provider")),
        model_active=bool(checks.get("model")),
        candles=bool(checks.get("candles")),
        broker_balance=bool(checks.get("broker_balance")),
        signal_identity=bool(checks.get("signal_identity")),
        snapshot_fresh=bool(checks.get("snapshot")),
        settlement_recovered=True,
        test_order=True,
        evidence=evidence,
        context=context,
        now=now,
    )
    receipt = {
        "drill_id": drill_id,
        "intent_id": drill["intent_id"],
        "order_id": recovery["order_id"],
        "settlement_status": recovery["settlement_status"],
        "recovery": evidence["recovery"],
        "context": drill["context"],
        "valid_for": {"created_at": now.isoformat(), "expires_at": (now + timedelta(hours=2)).isoformat()},
        "readiness": report,
    }
    with store.transaction():
        existing = store.test_receipt(drill_id)
        if existing is not None:
            if (existing["intent_id"], existing["body"]["order_id"], existing["body"]["settlement_status"]) != (
                receipt["intent_id"], receipt["order_id"], receipt["settlement_status"],
            ):
                raise ValueError("test receipt identity conflict")
            return existing
        stored = store.publish_test_receipt(
            drill_id=drill_id,
            intent_id=drill["intent_id"],
            body=receipt,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        store.set("readiness_evidence", evidence)
        store.set("readiness", report)
        return stored


def active_snapshot(store: Store) -> str:
    return active_snapshot_hash(store.active_pointers())


async def wait_for_drill_signal(client, config, drill, bounds, scan, *, stopped, now=utcnow):
    """Use the broker clock and wait within the existing single-use authorization."""
    from .executor import eligible_assets

    expires = datetime.fromisoformat(drill["expires_at"])
    processed = None
    while not stopped() and now() < expires:
        try:
            earliest, _ = bounds()
        except ValueError:
            await asyncio.sleep(0.5)
            continue
        if earliest >= expires:
            break
        boundary = earliest.replace(second=0, microsecond=0)
        if not (client.readiness.orders_ready and client.readiness.clock_ready
                and processed != boundary and 2 <= earliest.second < 50):
            await asyncio.sleep(0.5)
            continue
        processed = boundary
        try:
            async with asyncio.timeout(max(0, min(20, (expires - now()).total_seconds()))):
                inventory = eligible_assets(await client.market.list_assets(mode=None), config)
                native = next((a.symbol for a in inventory if canonicalize_asset(a.symbol, config.runtime.aliases) == drill["asset"]), None)
                if native is None:
                    continue
                signal = await scan(drill["asset"], native, boundary)
                if (signal is None or signal.symbol != drill["asset"]
                        or signal.candle_at != boundary - timedelta(minutes=1)):
                    continue
                balance = (await client.accounts.balance()).amount
                if not balance.is_finite() or balance <= 0:
                    raise ValueError("saldo TEST não confirmado ou indisponível")
        except (TransportError, TimeoutError):
            continue
        try:
            earliest, latest = bounds()
        except ValueError:
            continue
        if (not stopped() and now() < expires and latest < expires and earliest >= signal.decision_at
                and signal.sendable(latest, config.runtime.send_margin_seconds)):
            return signal, balance, native, latest
    return None


async def run_test_drill_once(
    store: Store,
    config: Any,
    *,
    drill_id: str,
    client: Any,
    signal: Any,
    bank,
    model_pointer: Mapping[str, Any],
    model_resolver: Any,
    model_providers: Any,
    account_binding: str,
    now: datetime | None = None,
    broker_symbol: str | None = None,
    record_executor_recovery: bool = True,
) -> dict[str, Any]:
    """Submit at most one signal through the normal Executor boundary."""
    from .executor import Executor

    now = now or utcnow()
    drill = store.test_drill(drill_id)
    if drill is None:
        raise ValueError("unknown test drill")
    if signal is None:
        if now >= datetime.fromisoformat(drill["expires_at"]):
            store.expire_test_drill(drill_id, reason="no_signal")
        return {"status": "no_signal", "drill": store.test_drill(drill_id)}
    executor = Executor(store, client, config)
    intent_id = await executor.submit(
        signal,
        bank,
        now=now,
        expected_version=None,
        model_pointer=dict(model_pointer),
        model_resolver=model_resolver,
        model_providers=model_providers,
        account_binding=account_binding,
        drill_id=drill_id,
        broker_symbol=broker_symbol,
    )
    current = store.test_drill(drill_id)
    if intent_id is None:
        return {"status": "blocked", "drill": current}
    recovery = None
    order = store.get(f"test_order:{drill_id}")
    if record_executor_recovery and current and current["status"] == "SETTLED" and order and order.get("status") in {"WIN", "LOSS", "LOSE", "TIE", "REFUNDED"}:
        recovery = record_recovery(
            store,
            drill_id=drill_id,
            intent_id=intent_id,
            order_id=order["order_id"],
            client_order_id=order.get("client_order_id"),
            settlement_status="LOSS" if order["status"] == "LOSE" else order["status"],
            source="executor_response",
            verified_at=now,
        )
    return {"status": "submitted", "intent_id": intent_id, "drill": store.test_drill(drill_id), "recovery": recovery}
