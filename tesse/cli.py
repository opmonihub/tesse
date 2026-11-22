"""Terminal operacional; credenciais locais ou entrada oculta, nunca em argumentos."""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from getpass import getpass, getuser
from pathlib import Path

import yaml

from .config import (
    SYMBOLS,
    Config,
    atomic_write,
    data_path,
    load_config,
    serialize_config,
    write_default,
)
from .core import utcnow
from .data import default_range, sync_symbol
from .outcomes import CANONICAL_OUTCOMES, normalize_outcome
from .state import Store
from .versioning import project_active, recover_projection, sync_manual


def emit(value):
    print(json.dumps(value, indent=2, default=str, ensure_ascii=False), flush=True)


def date_arg(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use AAAA-MM-DD em UTC") from exc


def interval(args):
    start, end = default_range()
    start, end = args.start or start, args.end or end
    if start >= end or end > utcnow().replace(hour=0, minute=0, second=0, microsecond=0):
        raise ValueError("Intervalo deve conter apenas dias UTC completos e encerrados")
    return start, end


def parser():
    p = argparse.ArgumentParser(
        prog="tesse",
        description="Tesse: executor TEST/OPTION/M1 e catalog de systems, models e adapters.",
        epilog="Roteamento diário por systems/models; receitas e aliases legados são somente leitura.",
    )
    p.add_argument("--config", type=Path, default=Path("bot.yml"))
    commands = p.add_subparsers(dest="command", required=True)
    from .forge.cli import register as register_forge

    register_forge(commands)
    strategies = commands.add_parser("strategies", help="Consultar catálogo e registro operacional")
    strategy_commands = strategies.add_subparsers(dest="strategy_command", required=True)
    strategy_list = strategy_commands.add_parser("list", help="Listar estratégias sem modificar o Store")
    strategy_list.add_argument("--json", action="store_true")
    commands.add_parser("init", help="Criar configuração, SQLite e chave privada do SDK")
    configure = commands.add_parser("configure", help="Ver YAML ou alterar um parâmetro manual")
    configure.add_argument("--set", metavar="CHAVE=VALOR")
    login = commands.add_parser("login", help="Autenticar com entrada oculta e sessão cifrada")
    login.add_argument("--two-factor", action="store_true")
    commands.add_parser("inventory", help="Consultar inventário nativo e aliases habilitados")
    data = commands.add_parser("data", help="Histórico Binance ou Ebinex")
    sub = data.add_subparsers(dest="data_command", required=True)
    ebinex = sub.add_parser("ebinex", help="Solicitar histórico Ebinex ao worker TEST")
    history = ebinex.add_subparsers(dest="history_action", required=True)
    probe = history.add_parser("probe", help="Sondar amostras em quatro horizontes")
    probe.add_argument("--as-of", type=date_arg, required=True)
    collect = history.add_parser("sync", help="Coletar um intervalo UTC completo")
    collect.add_argument("--start", type=date_arg, required=True)
    collect.add_argument("--end", type=date_arg, required=True)
    for command in (probe, collect):
        command.add_argument("--symbol", action="append", required=True, help="Símbolo nativo; repetível")
    history.add_parser("status", help="Consultar progresso por ID").add_argument("job_id")
    sync = sub.add_parser("sync", help="Baixar arquivos públicos Binance")
    sync.add_argument("--symbol", choices=SYMBOLS, help="Omitir baixa os dez ativos")
    for command in (sync,):
        command.add_argument("--start", type=date_arg)
        command.add_argument("--end", type=date_arg)
    start = commands.add_parser("start", help="Iniciar processo em segundo plano")
    start.add_argument(
        "--shadow", action="store_true",
        help="Simular DMI rápido com candles ao vivo; não enviar ordens",
    )
    commands.add_parser("worker", help="Executar o worker TEST em primeiro plano")
    commands.add_parser("resume", help="Retomar explicitamente um worker parado")
    status = commands.add_parser("status", help="Ver estado, versão e ordem pendente")
    status.add_argument("--watch", action="store_true")
    stop = commands.add_parser("stop", help="Parar novas entradas e acompanhar a ordem atual")
    stop.add_argument("--wait", type=int, default=10)
    report = commands.add_parser("report", help="Contabilidade e diagnóstico diário do bot")
    report.add_argument("--day", type=date_arg)
    commands.add_parser("versions", help="Listar perfis versionados")
    reset = commands.add_parser("reset-operation-lock", help="Liberar bloqueio manual com justificativa")
    reset.add_argument("--reason", required=True, help="Justificativa auditável; não inclua segredos")
    promote = commands.add_parser("promote", help="Promover um research bundle selado para models asset-scoped")
    promote.add_argument("research_run", type=Path)
    promote.add_argument("--approve", action="store_true", help="Autorizar explicitamente o registro deste bundle em TEST")
    promote.add_argument(
        "--weights", action="append", default=[], metavar="ASSET=PATH",
        help="Pesos por ativo, repetível; exemplo BTCUSDT=/tmp/model.weights",
    )
    demo = commands.add_parser("qualify-demo-dmi", help="Registrar candidato DMI BTCUSDT exclusivo TEST")
    demo.add_argument("run_manifest", type=Path)
    demo.add_argument("--approve", action="store_true", help="Aprovar o artefato exclusivamente para a conta demo")
    activate = commands.add_parser(
        "activate-model", help="Ativar explicitamente um modelo qualificado no Store TEST"
    )
    activate.add_argument("--asset", required=True)
    activate.add_argument("--model-id", required=True)
    activate.add_argument("--version", required=True, dest="model_version")
    activate.add_argument("--reason", required=True, help="Motivo auditável sem segredos")
    activate.add_argument("--demo-now", action="store_true",
                          help="Primeira ativação explícita do DMI BTCUSDT TEST sem esperar a virada UTC")
    rollback = commands.add_parser(
        "rollback", help="Restaurar perfil somente entre sessões, com worker parado"
    )
    rollback.add_argument("--version", help="Omitir restaura a ativação anterior")
    drill = commands.add_parser("test-drill", help="Autorizar e executar no máximo uma ordem TEST")
    drill_commands = drill.add_subparsers(dest="drill_command", required=True)
    plan = drill_commands.add_parser("plan", help="Criar plano sem abrir sessão ou enviar ordem")
    plan.add_argument("--asset", required=True)
    plan.add_argument("--ttl-minutes", type=int, default=15)
    run = drill_commands.add_parser("run", help="Confirmar e executar uma única tentativa")
    run.add_argument("drill_id_pos", nargs="?")
    run.add_argument("--drill-id")
    run.add_argument("--asset")
    run.add_argument("--confirm", required=True, dest="fingerprint")
    resume = drill_commands.add_parser("resume", help="Reconciliar uma tentativa sem reenviar")
    resume.add_argument("drill_id_pos", nargs="?")
    resume.add_argument("--drill-id")
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--shadow", action="store_true")
    return p


async def login(root, two_factor, env_path=None):
    from ebinex_sdk import Credentials

    from .credentials import credentials_from_env
    from .sdk import make_client
    # Both email and password hidden: shell history and process arguments never contain credentials.
    credentials = credentials_from_env(env_path or Path(".env"))
    if credentials is None:
        credentials = Credentials(email=getpass("E-mail (oculto): "), password=getpass("Senha: "))
    if two_factor:
        credentials = Credentials(
            email=credentials.email,
            password=credentials.password,
            code_2fa=getpass("Código 2FA: "),
        )
    client = make_client(root, orders=False)
    try:
        await client.auth.login(credentials)
        accounts = await client.accounts.list()
        test = next((a for a in accounts if a.environment == "TEST"), None)
        if test is None:
            raise ValueError("Nenhuma conta TEST disponível")
        await client.accounts.select(test.id)
        balance = await client.accounts.balance()
        emit(dict(authenticated=True, environment="TEST", balance=str(balance.amount)))
    finally:
        await client.disconnect()


async def inventory(root, config, env_path=None):
    from .credentials import credentials_from_env
    from .execution import eligible_assets
    from .sdk import make_client

    client = make_client(root)
    try:
        await client.auth.authenticate(credentials_from_env(env_path or Path(".env")))
        assets = await client.market.list_assets(mode=None)
        allowed = {a.symbol for a in eligible_assets(assets, config)}
        emit(
            [
                dict(
                    symbol=a.symbol,
                    market=a.market_status,
                    enabled=a.symbol in allowed,
                    research=config.runtime.aliases.get(a.symbol),
                )
                for a in assets
            ]
        )
    finally:
        await client.disconnect()




async def run_test_drill_cli(root, config, store, drill_id):
    from .broker_time import BrokerTime
    from .demo_dmi import RUNTIME_KEY as DEMO_DMI_RUNTIME_KEY
    from .execution.drill import run_test_drill_once, wait_for_drill_signal
    from .execution.readiness import readiness_report, store_evidence_context
    from .providers import build_runtime_provider_registry_from_arsenal
    from .registry.models import ModelArtifactRegistry
    from .runtime.active_snapshot import reconcile_active_snapshot
    from .runtime.resolver import ActiveModelResolver
    from .sdk import journal_snapshot, make_client

    drill = store.test_drill(drill_id)
    if drill is None or drill["status"] != "AUTHORIZED" or store.pending() or store.open_session():
        raise ValueError("test drill requires one authorized attempt and no open order or session")
    if config.runtime.environment != "TEST" or config.operation.environment != "TEST":
        raise ValueError("test drill requires TEST")
    binding, _ = await journal_snapshot(root)
    if binding != drill["account_binding"] or binding != store.get("account_binding"):
        raise ValueError("authenticated TEST account differs from verified observation")
    registry = ModelArtifactRegistry(root / "models")
    runtime = root / "runtime"
    resolver = ActiveModelResolver(runtime, registry, store=store)
    providers = build_runtime_provider_registry_from_arsenal(registry, store, operational=True)
    pointer = store.active_pointer(drill["asset"])
    context = store_evidence_context(config, store, binding, drill["asset"], utcnow())
    if not context or context != {key: value for key, value in drill["context"].items() if key != "drill"}:
        raise ValueError("test drill context changed")
    if reconcile_active_snapshot(store, runtime / "active.yml")["stale"]:
        raise ValueError("test drill requires a fresh verified active snapshot")
    card = resolver.resolve(drill["asset"])
    provider = providers.resolve(card) if card else None
    if provider is None:
        raise ValueError("test drill requires a verified runtime provider")
    clock = BrokerTime()
    client = make_client(root, orders=True, clock=clock)
    store.set("stop_requested", False)
    try:
        await client.connect()

        async def scan(asset, native, boundary):
            scope = {"native_symbol": native} if card.runtime_key == DEMO_DMI_RUNTIME_KEY else {}
            signal = await provider.scan(client, asset, card, store.get("active"), config.strategy.params(), boundary, **scope)
            return signal if signal and store.matches_signal_pointer(signal, pointer) else None

        candidate = await wait_for_drill_signal(
            client, config, drill, clock.bounds, scan, stopped=lambda: store.get("stop_requested", False),
        )
        if candidate is None:
            reason = "stopped" if store.get("stop_requested") else "no_signal"
            return {"status": reason, "drill": store.expire_test_drill(drill_id, reason=reason)}
        signal, balance, native, latest = candidate
        store.set("readiness", readiness_report(
            config, provider_healthy=provider.is_healthy(card), model_active=True,
            candles=True, broker_balance=True, signal_identity=store.matches_signal_pointer(signal, pointer),
            snapshot_fresh=True, context=context, now=latest,
        ))
        return await run_test_drill_once(
            store, config, drill_id=drill_id, client=client, signal=signal, bank=balance,
            model_pointer=pointer, model_resolver=resolver, model_providers=providers,
            account_binding=binding, now=latest, broker_symbol=native, record_executor_recovery=False,
        )
    finally:
        await client.disconnect()


async def resume_test_drill_cli(root, config, store, drill_id):
    from .execution.drill import publish_test_receipt, record_recovery
    from .execution.executor import Executor
    from .sdk import journal_snapshot, make_client
    from .state import intent_broker_symbol
    from .worker import restore_intent

    drill = store.test_drill(drill_id)
    if drill is None:
        raise ValueError("unknown test drill")
    pending = store.pending()
    if pending:
        linked = store.db.execute("SELECT drill_id FROM test_drill_intents WHERE intent_id=?", (pending["id"],)).fetchone()
        if not linked or linked[0] != drill_id:
            raise ValueError("another intent is pending; resume the matching drill")
    binding, entry = await journal_snapshot(root, pending["id"] if pending else None)
    if binding != drill["account_binding"]:
        raise ValueError("authenticated TEST account differs from drill")
    if entry is not None:
        restore_intent(store, entry)
    pending = store.pending()
    client = None
    try:
        if pending:
            client = make_client(root, orders=False)
            await client.connect()
            await Executor(store, client, config).reconcile()
        drill = store.test_drill(drill_id)
        recovery = store.get(f"test_recovery:{drill_id}")
        intent = store.db.execute("SELECT * FROM intents WHERE id=?", (drill.get("intent_id"),)).fetchone() if drill and drill.get("intent_id") else None
        order = store.get(f"test_order:{drill_id}")
        if (
            recovery is None
            and intent is not None
            and intent["state"] in {"WIN", "LOSS", "TIE", "REFUNDED"}
        ):
            # A process restart must corroborate the terminal result through the
            # read-only SDK reconciliation path; Store metadata alone is not a
            # broker-backed recovery proof.
            if client is None:
                client = make_client(root, orders=False)
                await client.connect()
            broker_id = intent["broker_id"] or (order or {}).get("order_id")
            client_id = intent["client_id"] or (order or {}).get("client_order_id")
            if broker_id or client_id:
                result = await client.orders.reconcile(
                    broker_order_id=broker_id, client_order_id=client_id,
                    symbols=(intent_broker_symbol(intent),), max_pages=10, timeout=10,
                )
                confirmed = getattr(result, "status", None) == "CONFIRMED" and getattr(result, "order", None) is not None
                if confirmed:
                    broker_order = result.order
                    request = getattr(broker_order, "request", None)
                    if (getattr(broker_order, "accepted_entry_at", None) != datetime.fromisoformat(intent["entry_at"])
                            or getattr(broker_order, "accepted_duration", None) != 60
                            or getattr(request, "symbol", None) != intent_broker_symbol(intent)
                            or getattr(request, "investment", None) != Decimal(intent["stake"])
                            or getattr(request, "direction", None) != intent["direction"]
                            or getattr(request, "mode", None) != "OPTION"
                            or getattr(request, "timeframe", None) != "M1"
                            or (broker_id and broker_order.id != broker_id)
                            or (client_id and broker_order.client_order_id != client_id)):
                        raise ValueError("broker settlement does not corroborate the intended order")
                    status = getattr(broker_order, "status", None)
                    status = "LOSS" if status == "LOSE" else status
                    if status == intent["state"] and getattr(broker_order, "id", None):
                        order = {
                            "order_id": broker_order.id,
                            "client_order_id": getattr(broker_order, "client_order_id", None),
                        }
                        store.set(f"test_order:{drill_id}", order)
                        recovery = record_recovery(
                            store, drill_id=drill_id, intent_id=intent["id"],
                            order_id=broker_order.id,
                            client_order_id=getattr(broker_order, "client_order_id", None),
                            settlement_status=status, source="sdk_reconcile",
                        )
        result = {"drill": store.test_drill(drill_id), "recovery": recovery, "pending": store.pending()}
        if result["drill"]["status"] == "RECEIPTED":
            result["receipt"] = store.test_receipt(drill_id)
        elif recovery is not None and result["drill"]["status"] == "SETTLED" and not store.get("halt_after_pending"):
            result["receipt"] = publish_test_receipt(store, config, drill_id=drill_id, recovery=recovery)
        return result
    finally:
        if client is not None:
            await client.disconnect()

def shadow_summary(store):
    if store.get("shadow_ledger") is None:
        return None
    return {**store.shadow_report(), "last_scan": store.get("shadow_last_scan")}


def daily_report(store, day):
    outcomes, errors, groups = Counter(), Counter(), defaultdict(lambda: Counter())
    for row in store.db.execute("SELECT * FROM intents WHERE substr(entry_at,1,10)=?", (str(day),)):
        state = row["state"]
        try:
            outcome = normalize_outcome(state)
        except ValueError:
            outcome = state  # Keep unknown historical states visible in the total.
        outcomes[outcome] += 1
        context = json.loads(row["context"])
        if outcome in CANONICAL_OUTCOMES and (adx := context.get("adx")) is not None:
            adx_start = int(adx // 5) * 5
            key = f"{row['symbol']} {row['direction']} ADX {adx_start}-{adx_start + 5}"
            groups[key][outcome] += 1
    for row in store.db.execute(
        "SELECT category,body FROM events WHERE substr(at,1,10)=?", (str(day),)
    ):
        if not row["category"].startswith("shadow_") and row["category"] not in {
            "strategy_result", "signal_selected", "signal_observed",
        }:
            errors[row["category"]] += 1
    return dict(
        day=day,
        outcomes=outcomes,
        technical_and_data_events=errors,
        contexts=dict(groups),
        interpretation="Descritivo experimental; perdas da estratégia não são falhas técnicas.",
    )


def write_json_report(directory, value, label):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / (label + ".json")
    atomic_write(output, json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n")
    return output


def market_history_cli(args):
    import sqlite3

    from .market_history import HistoryQueue, Request

    try:
        path = args.config.resolve()
        queue = HistoryQueue(data_path(path, load_config(path)))
        if args.history_action == "status":
            status = queue.status(args.job_id)
            if status is None:
                raise ValueError("Pedido de histórico Ebinex não encontrado")
        else:
            probe = args.history_action == "probe"
            end = args.as_of if probe else args.end
            if end > utcnow().replace(hour=0, minute=0, second=0, microsecond=0):
                raise ValueError("Use uma fronteira UTC que não inclua dias em andamento")
            request = Request(
                args.history_action, tuple(args.symbol),
                None if probe else int(args.start.timestamp()), int(end.timestamp()),
            )
            status = {"id": queue.enqueue(request)}
        emit(status)
        return 0
    except ValueError as exc:
        print(f"Histórico Ebinex: {exc}", file=sys.stderr)
    except (OSError, sqlite3.Error, yaml.YAMLError):
        print("Histórico Ebinex indisponível: confira a configuração e a fila local.", file=sys.stderr)
    return 1


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "data" and args.data_command == "ebinex":
        return market_history_cli(args)
    if args.command == "forge":
        from .forge.cli import dispatch

        return dispatch(args)
    if args.command == "strategies":
        from .registry.inventory import render_inventory, strategy_inventory

        try:
            result = strategy_inventory(args.config)
        except (OSError, ValueError) as exc:
            print(f"Catálogo indisponível: {exc}", file=sys.stderr)
            return 1
        (emit if args.json else render_inventory)(result)
        return 0
    from .sdk import journal_snapshot, storage_config
    from .worker import ProcessLock, is_running, run_worker, spawn_background
    path = args.config.resolve()
    root = None
    try:
        if args.command == "init":
            if not path.exists():
                write_default(path)
            config = load_config(path)
            root = data_path(path, config)
            storage_config(root, create=True)
            with Store(root / "state.sqlite") as store:
                store.ensure_baseline(config.strategy)
                store.set("config_fingerprint", config.fingerprint())
            emit(
                dict(
                    initialized=True,
                    config=str(path),
                    data=str(root),
                    profile="baseline_experimental",
                )
            )
            return 0
        config = load_config(path)
        root = data_path(path, config)
        if args.command in {"_worker", "worker"}:
            asyncio.run(run_worker(
                path,
                shadow=getattr(args, "shadow", False),
                foreground=args.command == "worker",
            ))
            return 0
        if args.command == "resume":
            from .container import clear_stop_request

            clear_stop_request(path)
            emit({"resumed": True, "config": str(path)})
            return 0
        if args.command == "configure":
            if (root / "state.sqlite").exists() and not is_running(root):
                with Store(root / "state.sqlite") as store:
                    recover_projection(store, path)
                config = load_config(path)
            if args.set:
                key, separator, value = args.set.partition("=")
                parts = key.split(".")
                if not separator or len(parts) < 2 or any(not part for part in parts):
                    raise ValueError(
                        "Use caminho.de.configuração=valor; exemplo "
                        "operation.stop_loss_split.entries=3"
                    )
                data = serialize_config(config)
                target = data
                for part in parts[:-1]:
                    if not isinstance(target, dict) or part not in target:
                        raise ValueError("Parâmetro desconhecido")
                    target = target[part]
                if not isinstance(target, dict) or parts[-1] not in target:
                    raise ValueError("Parâmetro desconhecido")
                target[parts[-1]] = yaml.safe_load(value)
                updated = Config.model_validate(data)
                atomic_write(
                    path, yaml.safe_dump(serialize_config(updated), sort_keys=False, allow_unicode=True)
                )
                config = updated
                if data_path(path, config) == root and not is_running(root):
                    with Store(root / "state.sqlite") as store:
                        store.ensure_baseline(config.strategy)
                        sync_manual(store, config)
            emit(serialize_config(config))
            return 0
        if args.command == "login":
            if is_running(root):
                raise ValueError("Pare o worker antes do login: o cofre tem posse exclusiva")
            asyncio.run(login(root, args.two_factor, path.resolve().parent / ".env"))
            return 0
        if args.command == "inventory":
            if is_running(root):
                raise ValueError("Pare o worker para consultar inventário com outro cliente")
            asyncio.run(inventory(root, config, path.resolve().parent / ".env"))
            return 0
        if args.command == "reset-operation-lock":
            db = root / "state.sqlite"
            if not db.is_file():
                raise ValueError("Nenhum Store operacional para liberar")
            actor = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"local:{getuser()}"
            with ProcessLock(root / "worker.lock"), Store(db) as store:
                previous = store.reset_operation_lock(actor=actor, reason=args.reason)
            emit({"reset": True, "previous": previous})
            return 0
        with Store(root / "state.sqlite") as store:
            store.ensure_baseline(config.strategy)
            if args.command == "test-drill":
                if is_running(root):
                    raise ValueError("Pare o worker antes de operar o test-drill")
                actor = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"local:{getuser()}"
                with ProcessLock(root / "worker.lock"):
                    if args.drill_command == "plan":
                        from .execution.drill import plan_test_drill
                        from .sdk import journal_snapshot
                        binding, _ = asyncio.run(journal_snapshot(root))
                        plan = plan_test_drill(
                            store, config, asset=args.asset, account_binding=binding, actor=actor,
                            now=utcnow(), ttl=timedelta(minutes=args.ttl_minutes),
                        )
                        emit(plan)
                    elif args.drill_command == "run":
                        from .execution.drill import authorize_test_drill
                        drill_id = args.drill_id or args.drill_id_pos
                        if not drill_id and args.asset:
                            row = store.db.execute(
                                "SELECT id FROM test_drills WHERE asset=? AND status='PLANNED' ORDER BY created_at DESC LIMIT 1",
                                (args.asset,),
                            ).fetchone()
                            drill_id = row[0] if row else None
                        if not drill_id:
                            raise ValueError("test drill id or asset is required")
                        authorize_test_drill(store, drill_id, fingerprint=args.fingerprint, actor=actor)
                        emit(asyncio.run(run_test_drill_cli(root, config, store, drill_id)))
                    else:
                        drill_id = args.drill_id or args.drill_id_pos
                        if not drill_id:
                            raise ValueError("test drill id is required")
                        emit(asyncio.run(resume_test_drill_cli(root, config, store, drill_id)))
                return 0
            if args.command == "data":
                start, end = interval(args)
                with ProcessLock(root / "data.lock"):
                    for symbol in [args.symbol] if args.symbol else SYMBOLS:
                        emit(sync_symbol(root, symbol, start, end))
            elif args.command == "start":
                if is_running(root):
                    raise ValueError("Worker já está ativo")
                if args.shadow and store.pending():
                    raise ValueError("shadow_requires_no_pending_broker_order")
                storage_config(root)
                binding, _ = asyncio.run(journal_snapshot(root))
                if not args.shadow and (
                    not binding or binding != store.get("account_binding", binding)
                ):
                    raise ValueError("Conta autenticada mudou; não misturar o histórico/Soros de outra conta")
                store.set("stop_requested", False)
                extra = ("--shadow",) if args.shadow else ()
                process = spawn_background(path, "_worker", root, extra=extra)
                for _ in range(30):
                    if process.poll() is not None:
                        raise ValueError(
                            "Worker encerrou; execute status e confira data/worker.log"
                        )
                    worker_state = store.get("worker") or {}
                    if (is_running(root) and worker_state.get("pid") == process.pid
                            and worker_state.get("state") not in {None, "stopped", "paused"}):
                        break
                    time.sleep(0.1)
                emit(dict(started=True, pid=process.pid, state=store.get("worker")))
            elif args.command == "stop":
                store.set("stop_requested", True)
                deadline = time.monotonic() + max(0, min(args.wait, 180))
                while is_running(root) and time.monotonic() < deadline:
                    time.sleep(0.5)
                emit(
                    dict(
                        running=is_running(root),
                        pending=store.pending(),
                        worker=store.get("worker"),
                    )
                )
            elif args.command == "status":
                while True:
                    emit(
                        dict(
                            running=is_running(root),
                            worker=store.get("worker"),
                            eligible_symbols=store.get("eligible_symbols", []),
                            readiness=store.get("readiness"),
                            broker_time=store.get("broker_time"),
                            review=store.get("review_status"),
                            learning=store.get("learning_status"),
                            shadow=shadow_summary(store),
                            **store.report(),
                        )
                    )
                    if not args.watch:
                        break
                    time.sleep(5)
            elif args.command == "report":
                day = args.day.date() if args.day else utcnow().date()
                report = dict(accounting=store.report(), daily=daily_report(store, day), shadow=shadow_summary(store))
                output = write_json_report(root / "reports", report, "daily-" + str(day))
                emit(report)
                print(f"Relatório: {output}")
            elif args.command == "versions":
                emit(dict(active=store.get("active"), versions=store.versions()))
            elif args.command == "qualify-demo-dmi":
                from .demo_dmi import qualify_demo_dmi
                from .registry.models import ModelArtifactRegistry

                if not args.approve:
                    raise ValueError("qualificação demo exige --approve")
                actor = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"local:{getuser()}"
                card = qualify_demo_dmi(args.run_manifest, root, ModelArtifactRegistry(root / "models"), store, actor=actor)
                earliest = datetime.fromisoformat(card.created_at_utc).astimezone(UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                ) + timedelta(days=1)
                emit({"qualified": True, "asset": card.asset, "model_id": card.model_id, "version": card.model_version,
                      "purpose": "TEST", "earliest_activation_utc": earliest, "holdout": card.metrics["holdout"]})
            elif args.command == "promote":
                from .models.providers import ModelProviderRegistry
                from .providers import RankEdgeM1Provider
                from .registry.models import ModelArtifactRegistry
                from .registry.promotion import promote_research_bundle, validate_research_bundle

                if not args.research_run.is_file():
                    emit({
                        "status": "blocked",
                        "reason": "missing_approved_bundle",
                        "system_id": "RANK-EDGE-M1-001",
                        "asset": "BTCUSDT",
                    })
                    return 1
                if not args.approve:
                    raise ValueError("Autorização explícita necessária: use --approve para este bundle")
                approver = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"local:{getuser()}"
                weights = {}
                for item in args.weights:
                    asset, separator, filename = item.partition("=")
                    if not separator or not asset or not filename:
                        raise ValueError("Use --weights ASSET=PATH")
                    if asset in weights:
                        raise ValueError(f"Pesos duplicados para {asset}")
                    weights[asset] = Path(filename).read_bytes()
                provider_resolver = ModelProviderRegistry()
                # This resolver validates canonical provider/card ownership at
                # promotion time; runtime health still requires loaded weights.
                provider_resolver.register(RankEdgeM1Provider())
                bundle = validate_research_bundle(args.research_run, provider_resolver=provider_resolver)
                if set(weights) != set(bundle.assets):
                    raise ValueError("Forneça --weights para cada ativo do research bundle")
                registry = ModelArtifactRegistry(root / "models")
                cards = promote_research_bundle(
                    bundle, registry, weights_by_asset=weights, store=store,
                    provider_resolver=provider_resolver, authorized_by=approver,
                )
                emit(dict(promoted=True, research_run=bundle.run_id, models=[card.model_id for card in cards]))
            elif args.command == "activate-model":
                if is_running(root):
                    raise ValueError("Pare o worker antes de ativar um modelo")
                from .providers import build_runtime_provider_registry
                from .registry.models import ModelArtifactRegistry
                from .runtime.activation import activate_model_explicit
                from .runtime.resolver import verified_model_card

                actor = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"local:{getuser()}"
                registry = ModelArtifactRegistry(root / "models")
                with ProcessLock(root / "worker.lock"):
                    if args.demo_now and (config.runtime.environment != "TEST" or config.operation.environment != "TEST"):
                        raise ValueError("--demo-now exige configuração TEST")
                    candidate = store.get_model_metadata(args.model_id, args.model_version)
                    if candidate is None or candidate["model_version"] != args.model_version:
                        raise ValueError("modelo ou versão não registrados")
                    card = verified_model_card(registry, args.model_id, asset=args.asset, metadata=candidate)
                    providers = build_runtime_provider_registry(
                        card, weights=registry.load_weights(card, card.asset), operational=True,
                    )
                    pointer = activate_model_explicit(
                        root / "runtime", registry, asset=args.asset, model_id=args.model_id,
                        model_version=args.model_version, store=store,
                        provider_resolver=providers, actor=actor, reason=args.reason,
                        demo_now=args.demo_now,
                    )
                emit({"activated": True, "asset": args.asset, "pointer": pointer, "actor": actor})
            elif args.command == "rollback":
                if is_running(root):
                    raise ValueError(
                        "Pare o worker antes do rollback; sessão Soros deve estar encerrada"
                    )
                recover_projection(store, path)
                config = load_config(path)
                if args.version:
                    store.promote(
                        args.version, expected_config=config.fingerprint(), cancel_pilot=True
                    )
                else:
                    store.rollback(expected_config=config.fingerprint())
                project_active(store, path, expected=config.fingerprint())
                emit(store.active_version())
        return 0
    except KeyboardInterrupt:
        print(
            "Interrompido. Intenções e sessões duráveis serão reconciliadas no reinício.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        # No SDK exception payload or traceback: may contain native/auth context.
        action = (
            str(exc)
            if isinstance(exc, (ValueError, FileNotFoundError))
            else {
                "AuthenticationError": "Execute login com o worker parado.",
                "InterventionRequiredError": "Autenticação exige 2FA/CAPTCHA; use o fluxo suportado pelo SDK.",
                "StorageError": "Preserve o journal. Confira ACL, chave original e limite de 4 MiB do SDK.",
                "StorageBusyError": "Outro processo usa o cofre; pare-o antes de autenticar.",
            }.get(
                type(exc).__name__,
                "Confira status/report; preserve banco, journal e chave antes de reiniciar.",
            )
        )
        print(f"{type(exc).__name__}: {action}", file=sys.stderr, flush=True)
        return 1
