"""Fronteira de execução. Intenção durável precede qualquer chamada de envio."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from ebinex_sdk import OrderRequest, OrderTiming
from ebinex_sdk.exceptions import (
    AuthenticationError,
    OrderDeadlineError,
    OrderScheduleMismatchError,
    OrderScheduleUnverifiedError,
    StorageError,
)

from ..config import effective_operation
from ..core import SessionUnavailable, utcnow
from ..models.assets import canonicalize_asset
from ..models.cards import card_hash
from ..outcomes import normalize_outcome
from ..state import intent_broker_symbol
from .readiness import is_test_environment, store_evidence_context


def eligible_assets(assets, config):
    result = []
    for asset in assets:
        mode = asset.modes.get("OPTION")
        if (
            config.runtime.accepts_symbol(asset.symbol)
            and asset.market_status == "OPEN"
            and mode
            and mode.status == "ACTIVE"
            and "M1" in mode.timeframes
        ):
            result.append(asset)
    return sorted(result, key=lambda a: a.symbol)


def advance_pilot(store, config):
    pilot = store.get("pilot")
    if not pilot or store.open_session() or store.pending():
        return False
    if pilot["failures"] or pilot["config_fingerprint"] != config.fingerprint():
        store.event(
            "pilot_rejected", version=pilot["version"], reason="technical_failure_or_config_change"
        )
        store.set("last_pilot", {**pilot, "status": "rejected"})
        store.set("pilot", None)
        return False
    if pilot["status"] == "waiting_boundary":
        pilot["status"] = "running"
        pilot["first_session_rowid"] = store.db.execute(
            "SELECT coalesce(max(rowid),0) FROM sessions"
        ).fetchone()[0]
    pilot["completed"] = store.db.execute(
        "SELECT count(*) FROM sessions WHERE pilot=1 AND complete=1 AND rowid>?",
        (pilot["first_session_rowid"],),
    ).fetchone()[0]
    if pilot["completed"] >= config.runtime.pilot_sessions:
        with store.transaction():
            store._activate(pilot["version"])
            store.set(
                "projection",
                dict(
                    params=json.loads(store.active_version()["params"]),
                    expected=config.fingerprint(),
                ),
            )
            store.set("qualified_master", True)
            store.set(
                "approved_versions",
                list(dict.fromkeys([*store.get("approved_versions", []), pilot["version"]])),
            )
            store.set("last_pilot", {**pilot, "status": "approved"})
            store.set("pilot", None)
            store.event("pilot_approved", version=pilot["version"])
        return True
    store.set("pilot", pilot)
    return False


class Executor:
    def __init__(self, store, client, config):
        self.store, self.client, self.config = store, client, config

    def fail_pilot(self):
        pilot = self.store.get("pilot")
        if pilot:
            pilot["failures"] += 1
            self.store.set("pilot", pilot)

    def _verify_identity(self, signal, pointer, resolver, providers, *, phase):
        valid = False
        if resolver is not None and providers is not None and self.store.matches_signal_pointer(signal, pointer):
            try:
                card = resolver.resolve(signal.symbol)
                if card is not None and card_hash(card) == pointer["card_hash"]:
                    provider = providers.resolve(card)
                    valid = provider is not None and provider.runtime_key == pointer["provider"]
            except (OSError, ValueError, AttributeError):
                valid = False
        if not valid:
            self.store.event("model_identity_changed", symbol=signal.symbol, phase=phase)
        return valid

    def _test_account_ready(self, binding):
        return bool(
            is_test_environment(self.config)
            and self.client.readiness.orders_ready
            and binding and binding == self.store.get("account_binding")
        )

    def _drill_ready(self, drill_id, signal, account_binding, now):
        drill = self.store.test_drill(drill_id) if drill_id else None
        if not drill or drill["status"] != "AUTHORIZED":
            return False
        if self.config.operation.environment != "TEST" or self.config.runtime.environment != "TEST":
            return False
        if not account_binding or drill["account_binding"] != account_binding:
            return False
        if drill["asset"] != signal.symbol or drill["model_id"] != signal.model_id:
            return False
        if not (datetime.fromisoformat(drill["confirmed_at"]) <= now < datetime.fromisoformat(drill["expires_at"])):
            return False
        current = store_evidence_context(self.config, self.store, account_binding, signal.symbol, now)
        confirmed = {key: value for key, value in drill["context"].items() if key != "drill"}
        if current != confirmed:
            return False
        readiness = getattr(self.client, "readiness", None)
        return bool(readiness and getattr(readiness, "orders_ready", False))

    async def submit(
        self, signal, bank, *, now=None, expected_version=None,
        model_pointer=None, model_resolver=None, model_providers=None, account_binding=None,
        drill_id=None, broker_symbol=None,
    ):
        now = now or utcnow()
        if self.store.pending() or self.store.get("stop_requested", False):
            return None
        if broker_symbol is not None and canonicalize_asset(broker_symbol, self.config.runtime.aliases) != signal.symbol:
            self.store.event("broker_symbol_mismatch", symbol=signal.symbol)
            return None
        if not signal.sendable(now, self.config.runtime.send_margin_seconds):
            self.store.event("late_signal", symbol=signal.symbol, target=signal.entry_at)
            return None
        if (
            not self.config.runtime.accepts_symbol(signal.symbol)
            or not self.client.readiness.orders_ready
        ):
            return None
        bank = Decimal(bank)
        if not bank.is_finite() or bank <= 0:
            return None
        advance_pilot(self.store, self.config)
        pilot = self.store.get("pilot")
        running_pilot = bool(pilot and pilot["status"] == "running")
        existing_session = self.store.open_session()
        selected_version = (
            self.store.restore(existing_session).version
            if existing_session
            else pilot["version"]
            if running_pilot
            else self.store.get("active")
        )
        if expected_version is not None and selected_version != expected_version:
            self.store.event(
                "version_changed_during_scan",
                symbol=signal.symbol,
                expected=expected_version,
                current=selected_version,
            )
            return None
        try:
            with self.store.transaction():
                if not self._verify_identity(
                    signal, model_pointer, model_resolver, model_providers, phase="before_reserve",
                ):
                    return None
                if drill_id is not None:
                    if not self._drill_ready(drill_id, signal, account_binding, now):
                        self.store.event("test_drill_context_blocked", symbol=signal.symbol, drill_id=drill_id, phase="reserve")
                        return None
                elif not self._test_account_ready(account_binding):
                    self.store.event("test_account_unready", symbol=signal.symbol, phase="reserve")
                    return None
                session = self.store.session(
                    bank,
                    Decimal(str(self.config.risk.session_fraction)),
                    version=pilot["version"] if running_pilot else None,
                    pilot=running_pilot,
                    operation=effective_operation(self.config),
                    entry_at=signal.entry_at,
                )
                if expected_version is not None and self.store.restore(session).version != expected_version:
                    self.store.event("version_changed_during_scan", symbol=signal.symbol, expected=expected_version)
                    return None
                identity = self.store.reserve(
                    signal, session, model_pointer=model_pointer, drill_id=drill_id,
                    account_binding=account_binding, now=now, broker_symbol=broker_symbol,
                )
                if identity is None and drill_id is not None:
                    raise SessionUnavailable("Sinal TEST já reservado ou não disponível")
        except SessionUnavailable as exc:
            self.store.event(
                "operation_limit_reached",
                symbol=signal.symbol,
                day=signal.entry_at.date().isoformat(),
                reason=str(exc),
            )
            return None
        if identity is None:
            return None
        reserved = self.store.pending()
        if not reserved or reserved["id"] != identity:
            return None
        request = OrderRequest(
            symbol=intent_broker_symbol(reserved),
            direction=signal.direction,
            investment=Decimal(reserved["stake"]),
            timeframe="M1",
            mode="OPTION",
        )
        timing = OrderTiming(
            signal_id=identity,
            entry_at=signal.entry_at,
            valid_until=signal.entry_at
            - timedelta(seconds=self.config.runtime.send_margin_seconds),
        )
        if self.store.get("stop_requested", False):
            self.store.settle(identity, "NOT_SENT", Decimal(0))
            return identity
        if not self._verify_identity(
            signal, model_pointer, model_resolver, model_providers, phase="before_submit",
        ):
            self.store.settle(identity, "NOT_SENT", Decimal(0))
            return None
        if drill_id is not None:
            drill = self.store.test_drill(drill_id)
            if not drill or drill["status"] != "PENDING":
                return None
        elif not self._test_account_ready(account_binding):
            self.store.settle(identity, "NOT_SENT", Decimal(0))
            self.store.event("test_account_unready", symbol=signal.symbol, phase="submit")
            return None
        try:
            order = await self.client.orders.place(request, timing=timing)
        except OrderDeadlineError:
            self.store.settle(identity, "NOT_SENT", Decimal(0))
            self.store.event("deadline_discarded", intent=identity)
            self.fail_pilot()
            return identity
        except (OrderScheduleMismatchError, OrderScheduleUnverifiedError) as exc:
            self.store.accept(identity, exc.order.id, exc.order.client_order_id)
            self.store.set("halt_after_pending", "Agenda aceita exige diagnóstico; consulte report")
            self.store.event("schedule_unverified", intent=identity, error_type=type(exc).__name__)
            self.fail_pilot()
            return identity
        except Exception as exc:
            # Generic exceptions may follow a committed send. Never guess NOT_SENT.
            self.store.mark_unknown(identity)
            if isinstance(exc, StorageError):
                self.store.set(
                    "halt_after_pending",
                    "Armazenamento SDK: preserve journal e chave; confira ACL e limite de 4 MiB antes de reiniciar",
                )
            elif isinstance(exc, AuthenticationError):
                self.store.set(
                    "halt_after_pending",
                    "Autenticação SDK inválida; execute login com o worker parado",
                )
            if drill_id is not None:
                self.store.mark_test_drill_unknown(drill_id, reason=type(exc).__name__)
            self.store.event("submission_unknown", intent=identity, error_type=type(exc).__name__)
            self.fail_pilot()
            return identity
        self.store.accept(identity, order.id, order.client_order_id)
        if drill_id is not None:
            self.store.set(f"test_order:{drill_id}", {
                "order_id": order.id, "client_order_id": order.client_order_id,
                "status": getattr(order, "status", None),
            })
        if order.accepted_entry_at != signal.entry_at or order.accepted_duration != 60:
            self.store.set("halt_after_pending", "Agenda divergente ou ainda não comprovada")
            self.fail_pilot()
        self.ingest(identity, order)
        return identity

    def ingest(self, identity, order):
        try:
            status = normalize_outcome(order.status, source="broker")
        except ValueError:
            return False
        if status not in {"WIN", "LOSS", "TIE", "REFUNDED", "CANCELED"}:
            return False
        if status == "WIN" and order.profit is None:
            self.store.mark_unknown(identity)
            self.store.event("missing_confirmed_profit", intent=identity)
            self.fail_pilot()
            return False
        if status == "CANCELED":
            self.fail_pilot()
        self.store.settle(identity, status, order.profit or Decimal(0))
        return True

    async def reconcile(self):
        row = self.store.pending()
        if not row:
            return True
        if not row["broker_id"] and not row["client_id"]:
            self.store.mark_unknown(row["id"])
            return False  # Worker closes SDK before reading its protected journal.
        result = await self.client.orders.reconcile(
            broker_order_id=row["broker_id"],
            client_order_id=row["client_id"],
            symbols=(intent_broker_symbol(row),),
            max_pages=10,
            timeout=10,
        )
        if result.status == "CONFIRMED" and result.order is not None:
            order = result.order
            self.store.accept(row["id"], order.id, order.client_order_id)
            drill_row = self.store.db.execute(
                "SELECT drill_id FROM test_drill_intents WHERE intent_id=?", (row["id"],)
            ).fetchone()
            if drill_row:
                self.store.set(f"test_order:{drill_row[0]}", {
                    "order_id": order.id, "client_order_id": order.client_order_id,
                    "status": getattr(order, "status", None),
                })
            intended = datetime.fromisoformat(row["entry_at"])
            if order.accepted_entry_at != intended or order.accepted_duration != 60:
                self.fail_pilot()
                self.store.set(
                    "halt_after_pending", "Reconciliação encontrou agenda não comprovada"
                )
            return self.ingest(row["id"], order)
        self.store.mark_unknown(row["id"])
        return False
