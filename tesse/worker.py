"""Executor único, sem serviço/autostart do Windows e sem ordens REAL."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ebinex_sdk.exceptions import StorageError, TransportError
from ebinex_sdk.services.market import CandleGapError

from .broker_time import BrokerTime
from .config import data_path, load_config
from .container import ContainerPreflightError, preflight_installation
from .core import DMI, Candle, Params, Signal, crossed, rank_signals, signals, utcnow
from .demo_dmi import RUNTIME_KEY as DEMO_DMI_RUNTIME_KEY
from .execution import Executor, advance_pilot, eligible_assets
from .execution.readiness import live_readiness_report
from .execution.shadow import ASSETS, PARAMS, VERSION, ShadowLedger
from .history_collector import HistoryCollector
from .models.assets import canonicalize_asset
from .models.cards import card_hash
from .providers import build_runtime_provider_registry_from_arsenal
from .registry.models import ModelArtifactRegistry
from .runtime.active_snapshot import reconcile_active_snapshot
from .runtime.resolver import ActiveModelResolver
from .sdk import journal_snapshot, make_client
from .state import Store, dumps, intent_broker_symbol
from .versioning import project_active, recover_projection, sync_manual


class ProcessLock:
    def __init__(self, path, *, timeout=0, create_parent=True):
        self.path, self.file = Path(path), None
        self.timeout = timeout
        self.create_parent = create_parent

    def __enter__(self):
        if self.create_parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    if self.path.stat().st_size == 0:
                        self.file.write(b"0")
                        self.file.flush()
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.file.close()
                    raise ValueError("Outro processo já possui a trava") from exc
                time.sleep(min(0.02, remaining))
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt

            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def is_running(root):
    try:
        with ProcessLock(root / "worker.lock"):
            return False
    except ValueError:
        return True


def _keep_intent_unknown(store, pending, reason):
    with store.transaction():
        store.mark_unknown(pending["id"])
        halt = f"Reconciliação pendente: {reason}; preserve journal e chave do SDK"
        if store.get("halt_after_pending") != halt:
            store.set("halt_after_pending", halt)
            store.event("intent_recovery_blocked", intent=pending["id"], reason=reason)


def restore_intent(store, entry):
    with store.transaction():
        pending = store.pending()
        if not pending:
            return
        if entry is None:
            _keep_intent_unknown(store, pending, "journal_missing")
            return
        state = getattr(entry, "state", None)
        broker_id = getattr(entry, "broker_order_id", None)
        client_id = getattr(entry, "client_order_id", None)
        request = getattr(entry, "request", None)
        entry_at = getattr(entry, "intended_entry_at", None)
        if (
            getattr(entry, "signal_id", None) != pending["id"]
            or not isinstance(client_id, str) or not client_id
            or getattr(request, "symbol", None) != intent_broker_symbol(pending)
            or getattr(request, "investment", None) != Decimal(pending["stake"])
            or str(getattr(request, "direction", None)) != pending["direction"]
            or getattr(request, "mode", None) != "OPTION"
            or getattr(request, "timeframe", None) != "M1"
            or entry_at is None or entry_at.isoformat() != pending["entry_at"]
            or pending["client_id"] not in (None, client_id)
            or pending["broker_id"] not in (None, broker_id)
        ):
            _keep_intent_unknown(store, pending, "journal_identity_incomplete_or_mismatched")
            return
        if state in {"NOT_SENT", "INTENDED"}:
            if pending["state"] != "DISPATCHING" or broker_id or pending["client_id"]:
                _keep_intent_unknown(store, pending, "journal_conflicts_with_possible_send")
            else:
                store.settle(pending["id"], "NOT_SENT", Decimal(0))
            return
        if state in {"DISPATCHING", "UNKNOWN"} and not broker_id:
            # A matching client identity allows read-only broker reconciliation, not a resend.
            store.remember_client_order(pending["id"], client_id)
            return
        if state not in {"ACCEPTED", "TERMINAL"} or not isinstance(broker_id, str) or not broker_id:
            _keep_intent_unknown(store, pending, "journal_identity_incomplete_or_mismatched")
            return
        store.accept(pending["id"], broker_id, client_id)
        if entry.order_snapshot:
            if (
                getattr(entry, "accepted_entry_at", None) is None
                or getattr(entry, "accepted_entry_at", None).isoformat() != pending["entry_at"]
                or getattr(entry, "accepted_duration", None) != 60
            ):
                store.set("halt_after_pending", "Agenda do journal divergente ou não comprovada")
                Executor(store, None, None).fail_pilot()
            drill_row = store.db.execute(
                "SELECT drill_id FROM test_drill_intents WHERE intent_id=?", (pending["id"],)
            ).fetchone()
            if drill_row:
                drill_id = drill_row[0]
                store.set(f"test_order:{drill_id}", {
                    "order_id": broker_id, "client_order_id": client_id,
                    "status": getattr(entry.order_snapshot, "status", None),
                })
            Executor(store, None, None).ingest(pending["id"], entry.order_snapshot)


async def _restore_worker_journal(store, root):
    pending = store.pending()
    try:
        binding, entry = await journal_snapshot(root, pending["id"] if pending else None)
    except (StorageError, OSError, ValueError) as exc:
        if pending:
            _keep_intent_unknown(store, pending, f"journal_unavailable:{type(exc).__name__}")
        raise
    if store.get("account_binding", binding) != binding:
        if pending:
            _keep_intent_unknown(store, pending, "account_binding_mismatch")
        raise ValueError("Conta autenticada mudou; não misturar o histórico/Soros de outra conta")
    store.set("account_binding", binding)
    restore_intent(store, entry)
    return binding


def spawn_background(config_path, command, root, *, extra=()):
    root.mkdir(parents=True, exist_ok=True)
    log = (root / ("worker.log" if command == "_worker" else "review.log")).open("ab")
    try:
        flags = (
            (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            if os.name == "nt"
            else 0
        )
        return subprocess.Popen(
            [sys.executable, "-m", "tesse", "--config", str(config_path), command, *extra],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    finally:
        log.close()


class LiveIndicators:
    def __init__(self):
        self.states = {}

    async def scan(self, client, symbol, version, params, boundary, model_id=None):
        key = (symbol, version, model_id)
        if key not in self.states:
            self.states = {k: v for k, v in self.states.items() if k[0] != symbol}
            self.states[key] = dict(dmi=DMI(params), previous=None, last=None)
        state = self.states[key]
        start = (
            max(state["last"] + timedelta(minutes=1), boundary - timedelta(minutes=500))
            if state["last"]
            else boundary - timedelta(minutes=500)
        )
        try:
            native = await client.market.get_candles(
                symbol, "M1", start=start, end=boundary, limit=500, mode="OPTION"
            )
        except Exception as exc:
            # SDK reports gaps with the actual candles attached; resets happen below.
            if type(exc).__name__ != "CandleGapError" or not hasattr(exc, "candles"):
                raise
            native = exc.candles
        selected = None
        for item in sorted(native, key=lambda c: c.timestamp):
            if item.timestamp >= boundary or (state["last"] and item.timestamp <= state["last"]):
                continue
            candle = Candle(
                item.timestamp,
                *(float(getattr(item, k)) for k in ("open", "high", "low", "close", "volume")),
            )
            if state["last"] and candle.timestamp != state["last"] + timedelta(minutes=1):
                state["previous"] = None
            current = state["dmi"].push(candle)
            direction = crossed(state["previous"], current, params.min_adx)
            if direction and candle.timestamp == boundary - timedelta(minutes=1):
                selected = Signal(
                    symbol,
                    candle.timestamp,
                    direction,
                    current.adx,
                    abs(current.plus - current.minus),
                )
            state["previous"], state["last"] = current, candle.timestamp
        return selected


M1 = timedelta(minutes=1)


async def _shadow_candles(client, native, start, end):
    limit = (end - start) // M1 + 1
    try:
        items = await asyncio.wait_for(
            client.market.get_candles(native, "M1", start=start, end=end, limit=limit, mode="OPTION"),
            timeout=10,
        )
    except CandleGapError as exc:
        items = exc.candles
    return {
        item.timestamp: Candle(
            item.timestamp, *(float(getattr(item, k)) for k in ("open", "high", "low", "close")),
            float(item.volume) if item.volume is not None else None,
        )
        for item in sorted(items, key=lambda item: item.timestamp) if start <= item.timestamp <= end
    }


async def shadow_tick(client, ledger, boundary, bounds):
    store, config = ledger.store, ledger.config
    assets = await asyncio.wait_for(client.market.list_assets(mode=None), timeout=10)
    native_by_asset = {}
    for item in eligible_assets(assets, config):
        try:
            asset = canonicalize_asset(item.symbol, config.runtime.aliases)
        except ValueError:
            continue
        if asset in ASSETS:
            native_by_asset.setdefault(asset, item.symbol)
    store.set("eligible_symbols", sorted(native_by_asset))
    start, end = boundary - 64 * M1, boundary - M1

    async def fetch(asset, native, first, last):
        try:
            return await _shadow_candles(client, native, first, last)
        except (TransportError, TimeoutError, ValueError) as exc:
            store.event("shadow_error", asset=asset, boundary=boundary, error_type=type(exc).__name__)
            return None

    batches = await asyncio.gather(*(
        fetch(asset, native, start, end) for asset, native in native_by_asset.items()
    ), return_exceptions=True)
    candles, found = {}, []
    for asset, batch in zip(native_by_asset, batches, strict=True):
        if isinstance(batch, BaseException):
            raise batch
        if batch is None:
            continue
        candles[asset] = batch
        if end not in batch:
            store.event("shadow_missing_candle", asset=asset, candle_at=end, reason="missing_signal_candle")
            continue
        found.extend(signal for signal in signals(asset, batch.values(), PARAMS) if signal.candle_at == end)

    settlement = {}
    for trade in store.shadow_trades(status="PENDING"):
        asset = trade["asset"]
        expiry = datetime.fromisoformat(trade["expiry_at"])
        entry = datetime.fromisoformat(trade["entry_at"])
        if expiry > boundary:
            continue
        native = ledger.pending_native_symbol(trade)
        if native is None:
            # Older pending trades did not persist the native symbol. They
            # may still be recovered from assets no longer open for entries.
            native = native_by_asset.get(asset) or next(
                (item.symbol for item in assets
                 if canonicalize_asset(item.symbol, config.runtime.aliases) == asset),
                None,
            )
        if native is None:
            continue
        if entry >= start and native == native_by_asset.get(asset) and asset in candles:
            batch = candles[asset]
        else:
            batch = await fetch(asset, native, entry - M1, entry)
        # One extra minute for publication delays. Transport failures never prove a gap.
        if batch is not None and (entry in batch or boundary >= expiry + M1):
            settlement[asset] = batch

    try:
        earliest, latest = bounds()
    except ValueError:
        for signal in found:
            ledger.record_signal(signal, version=VERSION, params=PARAMS, reason="broker_clock_unavailable")
        store.event("shadow_error", boundary=boundary, reason="broker_clock_unavailable")
        return
    if store.get("stop_requested"):
        return
    settled = ledger.settle_due(settlement, now=earliest)
    entries = []
    for signal in rank_signals(found):
        entries.append(ledger.record_signal(
            signal, version=VERSION, params=PARAMS, now=latest,
            native_symbol=native_by_asset[signal.symbol],
        ))
    observation = dict(
        boundary=boundary, assets=sorted(native_by_asset),
        closed_candles=sum(end in batch for batch in candles.values()), signals=len(found), version=VERSION,
    )
    store.set("shadow_last_scan", observation)
    store.event("shadow_scan", **observation)
    fields = ("id", "asset", "direction", "stake", "status", "net", "reason")
    print(dumps(dict(
        event="shadow_scan", **observation,
        entries=[{key: row[key] for key in fields} for row in entries],
        settlements=[{key: row[key] for key in fields} for row in settled],
    )), flush=True)


async def run_shadow_worker(config_path):
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    root = data_path(config_path, config)
    with ProcessLock(root / "worker.lock", timeout=1), Store(root / "state.sqlite") as store:
        def heartbeat(state, **extra):
            store.set("worker", dict(pid=os.getpid(), state=state, at=utcnow(), observe_only=True,
                                     shadow=True, version=VERSION, **extra))

        client = None
        try:
            if store.pending():
                raise ValueError("shadow_requires_no_pending_broker_order")
            heartbeat("starting")
            store.set("eligible_symbols", [])
            ledger = ShadowLedger(config, store)
            clock = BrokerTime()
            client = make_client(root, orders=False, clock=clock)
            await client.connect()
            store.event("shadow_started", pid=os.getpid(), version=VERSION)
            processed = None
            while not store.get("stop_requested"):
                ready = client.readiness
                store.set("sdk_readiness", ready.model_dump())
                try:
                    earliest, latest = clock.bounds()
                except ValueError:
                    store.set("broker_time", None)
                    heartbeat("shadow_waiting_clock")
                    await asyncio.sleep(1)
                    continue
                heartbeat("shadow_waiting_signal" if ready.clock_ready else "shadow_waiting_clock")
                store.set("broker_time", dict(earliest=earliest, latest=latest))
                boundary = earliest.replace(second=0, microsecond=0)
                if ready.clock_ready and processed != boundary and 2 <= earliest.second < 50:
                    processed = boundary
                    try:
                        await shadow_tick(client, ledger, boundary, clock.bounds)
                    except (TimeoutError, TransportError) as exc:
                        store.event("shadow_error", boundary=boundary, error_type=type(exc).__name__)
                await asyncio.sleep(1)
        except Exception as exc:
            store.event("shadow_error", error_type=type(exc).__name__)
            heartbeat("paused", error_type=type(exc).__name__)
            raise
        finally:
            if client is not None:
                await client.disconnect()
            if store.get("worker", {}).get("state") != "paused":
                heartbeat("stopped")


def _install_shutdown_handlers(store):
    """Ask the foreground worker to drain without persisting a false manual stop."""
    loop = asyncio.get_running_loop()
    requested = asyncio.Event()
    installed = []

    def request_shutdown(signum):
        # Keep the signal callback side-effect free: the Store may be closing
        # while the event-loop callback is drained during interpreter shutdown.
        if not requested.is_set():
            requested.set()

    for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, request_shutdown, signum.value)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            continue

    def stopping():
        return requested.is_set() or bool(store.get("stop_requested"))

    def cleanup():
        for signum in installed:
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                pass

    return stopping, cleanup


async def _connect_with_retry(client, store, stopping):
    delay = 0.5
    while True:
        if stopping():
            return False
        try:
            await client.connect()
            return True
        except (ConnectionError, OSError, TimeoutError, TransportError) as exc:
            store.set("worker", dict(pid=os.getpid(), state="waiting_connection", at=utcnow(),
                                     error_type=type(exc).__name__, retry_seconds=delay))
            store.event("worker_connection_retry", error_type=type(exc).__name__, retry_seconds=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def run_worker(config_path, *, observe_only=False, shadow=False, foreground=False):
    if shadow:
        if observe_only:
            raise ValueError("Escolha apenas um modo de observação")
        return await run_shadow_worker(config_path)
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    root = data_path(config_path, config)
    if foreground and not root.is_dir():
        raise ContainerPreflightError(f"Monte a raiz de dados existente: {root}")
    with ProcessLock(root / "worker.lock", timeout=1, create_parent=not foreground), ExitStack() as stack:
        if foreground:
            installation = preflight_installation(config_path, check_lock=False)
            if installation.data_root != root:
                raise ContainerPreflightError("data_directory mudou durante a partida")
            try:
                binding, entry = await journal_snapshot(root, installation.pending_id)
            except StorageError as exc:
                raise ContainerPreflightError("Não foi possível ler o cofre; confira a chave original e o journal") from exc
            if installation.account_binding != binding:
                raise ContainerPreflightError("Conta autenticada mudou ou não consta no banco operacional")
            if installation.pending_id and entry is None:
                raise ContainerPreflightError("Intenção pendente não consta no journal; preserve o cofre e o banco")
        store = stack.enter_context(Store(root / "state.sqlite"))
        store.ensure_baseline(config.strategy)
        if not store.pending():
            store.set("halt_after_pending", None)
        store.set("worker", dict(pid=os.getpid(), state="starting", at=utcnow()))
        client = None
        history = None
        def stopping():
            return bool(store.get("stop_requested"))

        def cleanup_signals():
            return None
        if foreground:
            stopping, cleanup_signals = _install_shutdown_handlers(store)
        try:
            recover_projection(store, config_path)
            config = load_config(config_path)
            sync_manual(store, config)
            if foreground:
                restore_intent(store, entry)
            else:
                binding = await _restore_worker_journal(store, root)
            broker_clock = BrokerTime()
            client = make_client(root, orders=not observe_only, clock=broker_clock)
            if foreground:
                if not await _connect_with_retry(client, store, stopping):
                    return
            else:
                await client.connect()
            engine = Executor(store, client, config)
            # A verified model is mandatory for production inference.  Missing cards
            # are explicit abstentions; no legacy DMI fallback is allowed.
            # Canonical model/runtime locations; SQLite remains authoritative.
            model_root = root / "models"
            model_registry = ModelArtifactRegistry(model_root)
            runtime_root = root / "runtime"
            runtime_root.mkdir(parents=True, exist_ok=True)
            snapshot_status = reconcile_active_snapshot(store, runtime_root / "active.yml")
            model_resolver = ActiveModelResolver(runtime_root, model_registry, store=store)
            # Runtime providers are rebuilt from the Store pointer and verified artifact
            # bytes on every process start. No loader/artifact means abstention.
            # build_runtime_provider_registry() remains the empty safe default
            # inside the reconstruction helper when no pointer is available.
            model_providers = build_runtime_provider_registry_from_arsenal(model_registry, store, operational=True)
            processed = None
            # Never submit a signal whose candle closed before this process
            # resumed. The first current boundary only warms indicators; the
            # next boundary is the first eligible post-restart signal.
            resume_floor = None
            stop_at = None
            history = HistoryCollector(root)
            history_idle = False

            def history_budget():
                if not history_idle or stopping() or store.pending():
                    return False
                try:
                    _, latest = broker_clock.bounds()
                except ValueError:
                    return False
                boundary = latest.replace(second=0, microsecond=0)
                return (processed == boundary
                        and (boundary + M1 - latest).total_seconds() >= history.timeout + 2)

            while True:
                history_idle = False
                now = utcnow()
                store.set(
                    "worker",
                    dict(
                        pid=os.getpid(),
                        state="reconciling" if store.pending() else "waiting_signal",
                        at=now,
                        observe_only=observe_only,
                    ),
                )
                store.set("sdk_readiness", client.readiness.model_dump())
                if stopping():
                    stop_at = stop_at or now
                    if not store.pending() or (now - stop_at).total_seconds() >= 180:
                        break
                if store.pending():
                    row = store.pending()
                    if not row["broker_id"] and not row["client_id"]:
                        # Release exclusive vault, retrieve identity, then restore new SDK client.
                        await history.pause("awaiting_session")
                        await client.disconnect()
                        client = None
                        binding = await _restore_worker_journal(store, root)
                        client = make_client(root, orders=not observe_only, clock=broker_clock)
                        if foreground:
                            if not await _connect_with_retry(client, store, stopping):
                                break
                        else:
                            await client.connect()
                        engine.client = client
                    await engine.reconcile()
                    await asyncio.sleep(2)
                    continue
                if store.get("halt_after_pending"):
                    raise ValueError(store.get("halt_after_pending"))
                if stop_at:
                    break
                config = load_config(config_path)
                if data_path(config_path, config) != root:
                    raise ValueError("Não mudar data_directory durante a execução; pare e reinicie")
                engine.config = config
                sync_manual(store, config)
                snapshot_status = reconcile_active_snapshot(store, runtime_root / "active.yml")
                # A promotion may commit while the worker is idle; rebuild from
                # the Store pointer before the next candle boundary.
                model_providers = build_runtime_provider_registry_from_arsenal(model_registry, store, operational=True)
                if advance_pilot(store, config):
                    project_active(store, config_path, expected=config.fingerprint())
                    config = load_config(config_path)
                    engine.config = config
                try:
                    earliest, latest = broker_clock.bounds()
                except ValueError:
                    await asyncio.sleep(0.5)
                    continue
                store.set("broker_time", dict(earliest=earliest, latest=latest))
                if not client.readiness.clock_ready:
                    await asyncio.sleep(0.5)
                    continue
                boundary = earliest.replace(second=0, microsecond=0)
                if foreground and resume_floor is None:
                    resume_floor = boundary + M1
                    processed = boundary
                    await asyncio.sleep(0.5)
                    continue
                if foreground and boundary < resume_floor:
                    await asyncio.sleep(0.5)
                    continue
                if processed != boundary and 2 <= earliest.second < 50:
                    processed = boundary
                    session = store.open_session()
                    pilot = store.get("pilot")
                    version = (
                        store.restore(session).version
                        if session
                        else pilot["version"]
                        if pilot and pilot["status"] == "running"
                        else store.get("active")
                    )
                    profile = store.db.execute(
                        "SELECT params FROM versions WHERE id=?", (version,)
                    ).fetchone()
                    params = Params(**json.loads(profile[0]))
                    assets = eligible_assets(await client.market.list_assets(mode=None), config)
                    by_canonical = {}
                    for asset in assets:
                        canonical = canonicalize_asset(asset.symbol, config.runtime.aliases)
                        by_canonical.setdefault(canonical, asset)
                    store.set("eligible_symbols", sorted(by_canonical))

                    async def scan(asset):
                        canonical_asset = canonicalize_asset(asset.symbol, config.runtime.aliases)
                        try:
                            pointer = store.active_pointer(canonical_asset)
                            card = model_resolver.resolve(canonical_asset)
                        except Exception as exc:
                            # A complete active pointer is a prerequisite for an
                            # model-backed signal; integrity failures never fall
                            # through to the legacy signal path.
                            engine.fail_pilot()
                            store.event(
                                "model_integrity_failure",
                                symbol=canonical_asset,
                                error_type=type(exc).__name__,
                            )
                            return None
                        if card is None or not pointer:
                            # No canonical model means no operational signal.
                            store.event("model_unavailable", symbol=asset.symbol)
                            # A missing clock after scan must be visible as a
                            # discarded/late signal, without selecting DMI.
                            try:
                                broker_clock.bounds()
                            except ValueError:
                                store.event("late_signal", symbol=asset.symbol, reason="broker_clock_unavailable")
                            return None
                        provider = model_providers.resolve(card)
                        if provider is None:
                            store.event(
                                "model_provider_unavailable",
                                symbol=canonical_asset,
                                model_id=card.model_id,
                            )
                            return None
                        scan_method = getattr(provider, "scan", None)
                        if not callable(scan_method):
                            store.event(
                                "model_provider_unavailable",
                                symbol=canonical_asset,
                                model_id=card.model_id,
                            )
                            return None
                        try:
                            market_scope = {"native_symbol": asset.symbol} if card.runtime_key == DEMO_DMI_RUNTIME_KEY else {}
                            signal = await asyncio.wait_for(
                                scan_method(client, canonical_asset, card, version, params, boundary, **market_scope),
                                timeout=20,
                            )
                            if signal is None:
                                return None
                            if foreground and signal.candle_at != boundary - M1:
                                return None
                            if signal.symbol != canonical_asset:
                                raise ValueError("model provider returned a different asset")
                            # Preserve the exact model identity used for the
                            # signal so Store.reserve() persists it in context.
                            from dataclasses import replace

                            signal = replace(
                                signal,
                                model_id=card.model_id,
                                card_sha256=card_hash(card),
                            )
                            if not store.matches_signal_pointer(signal, pointer):
                                store.event("model_identity_changed", symbol=canonical_asset, phase="scan")
                                return None
                            return signal, card.model_id, card, provider, pointer
                        except Exception as exc:
                            engine.fail_pilot()
                            store.event(
                                "market_data_failure",
                                symbol=canonical_asset,
                                error_type=type(exc).__name__,
                            )
                            return None

                    outcomes = await asyncio.gather(*(scan(a) for a in by_canonical.values()))
                    model_outcomes = [item for item in outcomes if item]
                    found = rank_signals([item[0] for item in model_outcomes if item[0]])
                    model_ids = {item[0].symbol: item[1] for item in model_outcomes if item[0]}
                    cards_by_symbol = {item[0].symbol: item[2] for item in model_outcomes if item[0]}
                    providers_by_symbol = {item[0].symbol: item[3] for item in model_outcomes if item[0]}
                    pointers_by_symbol = {item[0].symbol: item[4] for item in model_outcomes}
                    for signal in found:
                        store.event(
                            "signal_observed",
                            symbol=signal.symbol,
                            direction=signal.direction,
                            adx=signal.adx,
                            distance=signal.distance,
                            target=signal.entry_at,
                            version=version,
                            model_id=model_ids.get(signal.symbol),
                        )
                    selected = found[0] if found else None
                    selected_card = cards_by_symbol.get(selected.symbol) if selected else None
                    selected_provider = providers_by_symbol.get(selected.symbol) if selected else None
                    identity_ok = bool(
                        selected and selected_card and selected_provider
                        and selected.model_id == selected_card.model_id
                        and selected.card_sha256 == card_hash(selected_card)
                    )
                    bank = None
                    balance_ready = False
                    if selected:
                        if not observe_only or selected_card.runtime_key == DEMO_DMI_RUNTIME_KEY:
                            # Native balance before opening an order; never substitute simulated bank.
                            bank = (await client.accounts.balance()).amount
                            balance_ready = bank.is_finite() and bank > 0
                        else:
                            balance_ready = bool(getattr(client.readiness, "account_ready", False))
                    report = live_readiness_report(
                        config,
                        provider_healthy=bool(selected_provider and selected_card and selected_provider.is_healthy(selected_card)),
                        model_active=bool(selected_card),
                        candles=bool(assets),
                        broker_balance=balance_ready,
                        signal_identity=identity_ok,
                        snapshot_fresh=not snapshot_status.get("stale", True),
                        sdk_ready=client.readiness.orders_ready,
                        observe_only=observe_only,
                    )
                    store.set("readiness", report)
                    if stopping():
                        break
                    if report["state"] == "test_order_ready":
                        try:
                            latest = broker_clock.bounds()[1]
                        except ValueError:
                            store.event("late_signal", symbol=selected.symbol, reason="broker_clock_unavailable")
                        else:
                            await engine.submit(
                                selected, bank, now=latest, expected_version=version,
                                model_pointer=pointers_by_symbol[selected.symbol],
                                model_resolver=model_resolver, model_providers=model_providers,
                                account_binding=binding,
                                broker_symbol=by_canonical[selected.symbol].symbol,
                            )
                history_idle = True
                history.offer(client, binding, history_budget)
                await asyncio.sleep(0.5)
        except Exception as exc:
            Executor(store, None, None).fail_pilot()
            store.event("worker_paused", error_type=type(exc).__name__)
            store.set(
                "worker",
                dict(
                    pid=os.getpid(),
                    state="paused",
                    at=utcnow(),
                    error_type=type(exc).__name__,
                    action=str(exc)
                    if isinstance(exc, ValueError)
                    else "Confira report/logs. Autenticação: execute login; armazenamento: preserve journal/chave e confira ACL/4 MiB; transporte: confira conexão e reinicie para reconciliar.",
                ),
            )
            raise
        finally:
            cleanup_signals()
            if history is not None:
                await history.pause("awaiting_worker")
            if client:
                await client.disconnect()
            current = store.get("worker", {})
            if current.get("state") != "paused":
                store.set("worker", dict(pid=os.getpid(), state="stopped", at=utcnow()))
