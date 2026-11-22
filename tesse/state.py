"""Transações duráveis. Nunca tentar republicar uma intenção após um crash."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .config import Strategy, params_dict
from .core import Session, SessionUnavailable, operational_day, utcnow
from .migration import reject_legacy_artifact
from .outcomes import normalize_outcome
from .registry.store_schema import ensure_versioned_model_schema


def dumps(value):
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)


def intent_broker_symbol(row):
    return json.loads(row["context"]).get("broker_symbol", row["symbol"])


class Store:
    def __init__(self, path: Path, *, model_root: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._tx_depth = 0
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,params TEXT NOT NULL,
            created TEXT NOT NULL,kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activations(seq INTEGER PRIMARY KEY,version TEXT NOT NULL,
            created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,body TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,pilot INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS one_session ON sessions(complete) WHERE complete=0;
        CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
            symbol TEXT NOT NULL,direction TEXT NOT NULL,candle_at TEXT NOT NULL,
            entry_at TEXT NOT NULL,stake TEXT NOT NULL,state TEXT NOT NULL,
            broker_id TEXT,client_id TEXT,profit TEXT,net TEXT,context TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON intents((1))
            WHERE state IN ('DISPATCHING','ACCEPTED','UNKNOWN');
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,
            category TEXT NOT NULL,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY,created TEXT NOT NULL,
            body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS routing_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL, decision_day TEXT NOT NULL,
            selected_model_id TEXT, reason TEXT NOT NULL, score_config TEXT NOT NULL,
            gates TEXT NOT NULL, scores TEXT NOT NULL, evidence_window TEXT NOT NULL,
            card_hashes TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(asset, decision_day));
        CREATE TABLE IF NOT EXISTS routing_decision_details(
            routing_id INTEGER PRIMARY KEY REFERENCES routing_decisions(id) ON DELETE CASCADE,
            decision_sha256 TEXT NOT NULL, identities TEXT NOT NULL, status TEXT NOT NULL,
            system_id TEXT);
        CREATE TABLE IF NOT EXISTS promotion_transactions(
            id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, status TEXT NOT NULL,
            bundle_hash TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, committed_at TEXT);
        CREATE TABLE IF NOT EXISTS recovery_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, resolved_at TEXT);
        CREATE TABLE IF NOT EXISTS promotion_receipts(
            operation_id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, bundle_hash TEXT NOT NULL,
            status TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS test_drills(
            id TEXT PRIMARY KEY, asset TEXT NOT NULL, model_id TEXT NOT NULL,
            model_version TEXT NOT NULL, card_hash TEXT NOT NULL, weights_hash TEXT NOT NULL,
            provider TEXT NOT NULL, account_binding TEXT NOT NULL, context TEXT NOT NULL,
            fingerprint TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, status TEXT NOT NULL, confirmed_at TEXT,
            consumed_at TEXT, intent_id TEXT, result TEXT, receipt_id TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_test_drill
            ON test_drills(asset) WHERE status IN ('PLANNED','AUTHORIZED','CONSUMED','PENDING','UNKNOWN');
        CREATE TABLE IF NOT EXISTS test_drill_intents(
            drill_id TEXT PRIMARY KEY REFERENCES test_drills(id),
            intent_id TEXT NOT NULL UNIQUE REFERENCES intents(id)
        );
        CREATE TABLE IF NOT EXISTS test_receipts(
            id TEXT PRIMARY KEY, drill_id TEXT NOT NULL UNIQUE REFERENCES test_drills(id),
            intent_id TEXT NOT NULL UNIQUE REFERENCES intents(id), body TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_trades(
            id TEXT PRIMARY KEY, asset TEXT NOT NULL, candle_at TEXT NOT NULL,
            entry_at TEXT NOT NULL, expiry_at TEXT NOT NULL, direction TEXT NOT NULL,
            adx TEXT NOT NULL, distance TEXT NOT NULL, stake TEXT NOT NULL,
            status TEXT NOT NULL, net TEXT NOT NULL, version TEXT NOT NULL,
            params TEXT NOT NULL, created_at TEXT NOT NULL, settled_at TEXT, reason TEXT,
            UNIQUE(asset, candle_at)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_shadow ON shadow_trades(status)
            WHERE status='PENDING';
        """)
        try:
            ensure_versioned_model_schema(self, model_root or (path.parent / "models"))
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        """One logical transaction; nested Store APIs reuse the outer BEGIN."""
        outer = self._tx_depth == 0
        if outer:
            self.db.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            if outer:
                self.db.rollback()
            raise
        else:
            if outer:
                self.db.commit()
        finally:
            self._tx_depth -= 1

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))

    def event(self, category, **context):
        reject_legacy_artifact(context, kind="event")
        self.db.execute(
            "INSERT INTO events(at,category,body) VALUES(?,?,?)",
            (utcnow().isoformat(), category, dumps(context)),
        )

    def shadow_signal(self, signal):
        row = self.db.execute(
            "SELECT * FROM shadow_trades WHERE asset=? AND candle_at=?",
            (signal.symbol, signal.candle_at.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    def record_shadow_signal(self, signal, *, stake, version, params, reason=None):
        """Persist one virtual signal without touching operational intents."""
        if not getattr(signal, "symbol", None) or not getattr(signal, "candle_at", None):
            raise ValueError("shadow signal identity is required")
        stake = Decimal(str(stake))
        if not stake.is_finite() or (stake != 0 if reason else stake < 1):
            raise ValueError("invalid virtual stake for signal disposition")
        payload = params_dict(params)
        with self.transaction():
            existing = self.shadow_signal(signal)
            if existing is not None:
                return existing
            trade_id = str(uuid.uuid4())
            now = utcnow().isoformat()
            status = "SKIPPED" if reason else "PENDING"
            self.db.execute(
                "INSERT INTO shadow_trades(id,asset,candle_at,entry_at,expiry_at,direction,adx,distance,stake,status,net,version,params,created_at,settled_at,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_id, signal.symbol, signal.candle_at.isoformat(),
                    signal.entry_at.isoformat(), signal.expiry_at.isoformat(), signal.direction,
                    str(signal.adx), str(signal.distance), str(stake), status, "0",
                    version, dumps(payload), now, None, reason,
                ),
            )
            self.event(
                "shadow_signal", trade_id=trade_id, asset=signal.symbol, candle_at=signal.candle_at,
                entry_at=signal.entry_at, expiry_at=signal.expiry_at, direction=signal.direction,
                version=version, stake=str(stake), status=status, reason=reason,
            )
            return dict(self.db.execute("SELECT * FROM shadow_trades WHERE id=?", (trade_id,)).fetchone())

    def shadow_trades(self, *, status=None):
        query = "SELECT * FROM shadow_trades"
        params = []
        if status is not None:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY candle_at, id"
        return [dict(row) for row in self.db.execute(query, params)]

    def settle_shadow_trade(self, trade_id, *, status, net, settled_at, reason=None):
        if status not in {"WIN", "LOSS", "TIE", "MISSING_CANDLE"}:
            raise ValueError("invalid shadow settlement")
        net = Decimal(str(net))
        if not net.is_finite():
            raise ValueError("shadow net must be finite")
        with self.transaction():
            row = self.db.execute("SELECT * FROM shadow_trades WHERE id=?", (trade_id,)).fetchone()
            if row is None:
                raise ValueError("unknown shadow trade")
            if row["status"] != "PENDING":
                return dict(row)
            self.db.execute(
                "UPDATE shadow_trades SET status=?,net=?,settled_at=?,reason=? WHERE id=?",
                (status, str(net), settled_at.isoformat(), reason, trade_id),
            )
            category = "shadow_missing_candle" if status == "MISSING_CANDLE" else "shadow_settlement"
            self.event(category, trade_id=trade_id, asset=row["asset"], status=status, net=str(net), reason=reason)
            return dict(self.db.execute("SELECT * FROM shadow_trades WHERE id=?", (trade_id,)).fetchone())

    def shadow_report(self):
        with self.transaction():
            return self._shadow_report()

    def _shadow_report(self):
        ledger = self.get("shadow_ledger") or {}
        counts = dict(self.db.execute("SELECT status,count(*) FROM shadow_trades GROUP BY status"))
        exclusions = dict(self.db.execute(
            "SELECT reason,count(*) FROM shadow_trades WHERE status IN ('SKIPPED','MISSING_CANDLE') GROUP BY reason"
        ))
        latest = self.db.execute(
            "SELECT * FROM shadow_trades WHERE status in ('WIN','LOSS','TIE') ORDER BY settled_at DESC LIMIT 1"
        ).fetchone()
        initial = Decimal(ledger.get("initial_bank", "0"))
        bank = Decimal(ledger.get("virtual_bank", "0"))
        total = sum(counts.values())
        return {
            "counts": counts,
            "total": total,
            "virtual_trades": total - counts.get("SKIPPED", 0),
            "pending": counts.get("PENDING", 0),
            "net": str(bank - initial),
            "virtual_bank": str(bank),
            "initial_bank": str(initial),
            "exclusions": exclusions,
            "pending_trade": next(iter(self.shadow_trades(status="PENDING")), None),
            "last_settlement": dict(latest) if latest else None,
        }

    def add_version(self, params, kind="candidate"):
        body = dumps(Strategy(**params_dict(params)).model_dump())
        identity = hashlib.sha256(body.encode()).hexdigest()[:16]
        self.db.execute(
            "INSERT OR IGNORE INTO versions VALUES(?,?,?,?)",
            (identity, body, utcnow().isoformat(), kind),
        )
        return identity

    def ensure_baseline(self, params):
        with self.transaction():
            identity = self.get("active")
            if identity is None:
                identity = self.add_version(params, "baseline_experimental")
                self._activate(identity)
            return identity

    def _activate(self, identity):
        if not self.db.execute("SELECT 1 FROM versions WHERE id=?", (identity,)).fetchone():
            raise ValueError("Versão inexistente")
        self.set("active", identity)
        self.set("qualified_master", identity in self.get("approved_versions", []))
        self.db.execute(
            "INSERT INTO activations(version,created) VALUES(?,?)", (identity, utcnow().isoformat())
        )

    def active_version(self):
        return dict(
            self.db.execute("SELECT * FROM versions WHERE id=?", (self.get("active"),)).fetchone()
        )

    def versions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM versions ORDER BY created")]

    def promote(self, identity, *, expected_config=None, cancel_pilot=False, experimental=False):
        with self.transaction():
            if self.open_session() or self.pending():
                raise ValueError("Troca permitida somente entre sessões")
            self._activate(identity)
            if cancel_pilot:
                self.set("pilot", None)
            if experimental:
                self.set("qualified_master", False)
            if expected_config:
                self.set(
                    "projection",
                    dict(
                        params=json.loads(self.active_version()["params"]), expected=expected_config
                    ),
                )
            self.event("version_promoted", version=identity)

    def rollback(self, *, expected_config=None):
        rows = self.db.execute(
            "SELECT version FROM activations ORDER BY seq DESC LIMIT 2"
        ).fetchall()
        if len(rows) < 2:
            raise ValueError("Não há versão anterior")
        self.promote(rows[1][0], expected_config=expected_config, cancel_pilot=True)

    def open_session(self):
        row = self.db.execute("SELECT * FROM sessions WHERE complete=0").fetchone()
        return dict(row) if row else None

    def _calendar_day_results(self, session_day: str) -> tuple[Decimal, Decimal]:
        losses = net = Decimal(0)
        rows = self.db.execute(
            "SELECT i.state, i.net, s.body FROM intents i JOIN sessions s ON s.id=i.session_id "
            "WHERE i.state IN ('WIN','LOSS','LOSE','TIE')"
        )
        for row in rows:
            body = json.loads(row["body"])
            if body.get("session_day") != session_day:
                continue
            amount = Decimal(str(row["net"] or "0"))
            net += amount
            if row["state"] in {"LOSS", "LOSE"} and amount < 0:
                losses -= amount
        return losses, net

    def reset_operation_lock(self, *, actor: str, reason: str) -> dict:
        if not actor.strip() or not reason.strip():
            raise ValueError("O reset exige operador e justificativa")
        with self.transaction():
            if self.open_session() or self.pending():
                raise ValueError("Reset exige sessão encerrada e nenhuma intenção pendente")
            lock = self.get("operation_lock")
            if not lock or lock.get("policy") != "manual_reset":
                raise ValueError("Nenhum bloqueio manual para liberar")
            self.set("operation_lock", None)
            self.event("operation_lock_reset", actor=actor, reason=reason.strip(), previous=lock)
            return lock

    def session(
        self,
        bank: Decimal,
        fraction=Decimal("0.05"),
        version=None,
        pilot=False,
        *,
        operation=None,
        session_day=None,
        entry_at=None,
    ):
        with self.transaction():
            row = self.open_session()
            if row and self.restore(row).demo_continuous:
                raise SessionUnavailable("Sessão demo legada só admite reconciliação")
            if operation is None:
                if row:
                    return row
                if (self.get("operation_lock") or {}).get("policy") == "manual_reset":
                    raise SessionUnavailable("Sessão bloqueada: manual_reset")
                session = Session.begin(bank, version or self.get("active"), fraction)
            else:
                if entry_at is not None:
                    window = operation.session
                    actual_day = operational_day(
                        entry_at, window.timezone, window.start, window.end
                    )
                    if session_day is not None and session_day != actual_day:
                        raise SessionUnavailable("Dia informado diverge do calendário operacional")
                    session_day = actual_day
                if (
                    not isinstance(session_day, str)
                    or date.fromisoformat(session_day).isoformat() != session_day
                ):
                    raise ValueError("session_day deve usar AAAA-MM-DD para o perfil operacional")
                lock = self.get("operation_lock")
                if lock and (
                    lock.get("policy") == "manual_reset" or lock.get("day") == session_day
                ):
                    policy = lock.get("policy", "next_day")
                    raise SessionUnavailable(f"Sessão bloqueada: {lock.get('reason')} ({policy})")
                fingerprint = hashlib.sha256(dumps(operation.model_dump()).encode()).hexdigest()
                if row:
                    previous = self.restore(row)
                    if previous.session_day == session_day:
                        if previous.operation_fingerprint != fingerprint:
                            raise SessionUnavailable("Sessão aberta usa perfil operacional diferente")
                        return row
                    if self.pending():
                        raise SessionUnavailable("Sessão anterior com intenção pendente")
                    if not previous.session_day or previous.session_day > session_day:
                        raise SessionUnavailable("Sessão aberta incompatível com o dia operacional")
                    previous.complete, previous.close_reason = True, "session_day_ended"
                    self.db.execute(
                        "UPDATE sessions SET body=?, complete=1 WHERE id=?",
                        (dumps(asdict(previous)), row["id"]),
                    )
                    self.event(
                        "operation_session_expired", session=row["id"], day=previous.session_day
                    )
                active_count = self.db.execute(
                    "SELECT count(*) FROM sessions WHERE json_extract(body, '$.session_day')=?",
                    (session_day,),
                ).fetchone()[0]
                if active_count >= operation.session.max_sessions_per_day:
                    raise SessionUnavailable(f"Máximo de sessões atingido em {session_day}")
                session = Session.begin_operational(
                    bank, version or self.get("active"), operation, session_day
                )
                session.operation_fingerprint = fingerprint
                daily_losses, daily_net = self._calendar_day_results(session_day)
                if session.stop_loss_scope == "calendar_day":
                    remaining = session.stop_loss - daily_losses
                    if remaining < 1:
                        raise SessionUnavailable(f"Sessão bloqueada em {session_day}: stop_loss")
                    session.stop_loss = remaining
                    session.max_entries = min(session.max_entries, int(remaining))
                if session.stop_win_scope == "calendar_day":
                    session.starting_day_net = daily_net
                    if session.stop_when_limit_reached and daily_net >= session.stop_win:
                        raise SessionUnavailable(f"Sessão bloqueada em {session_day}: stop_win")
                session.stake = session.bounded_stake()
                if session.stake < 1:
                    raise SessionUnavailable(
                        f"Saldo diário insuficiente em {session_day}: stop_loss"
                    )
            identity = str(uuid.uuid4())
            body = dumps(asdict(session))
            self.db.execute("INSERT INTO sessions VALUES(?,?,0,?)", (identity, body, int(pilot)))
            return dict(id=identity, body=body, complete=0, pilot=int(pilot))

    @staticmethod
    def _hash(value: str, label: str) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"{label} must be a SHA-256 hex digest")
        try:
            int(value, 16)
        except ValueError as exc:
            raise ValueError(f"{label} must be a SHA-256 hex digest") from exc
        return value

    def legacy_model_version(self, model_id: str) -> str | None:
        row = self.db.execute(
            "SELECT model_version FROM legacy_model_bindings WHERE model_id=?", (model_id,)
        ).fetchone()
        return row[0] if row else None

    def get_model_metadata(self, model_id: str, model_version: str | None = None) -> dict | None:
        query, params = "SELECT * FROM model_metadata WHERE model_id=?", (model_id,)
        if model_version is not None:
            query += " AND model_version=?"
            params += (model_version,)
        rows = self.db.execute(query, params).fetchmany(2)
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError(f"ambiguous model ID: {model_id}; specify model_version")
        return self._metadata_dict(rows[0])

    @staticmethod
    def _metadata_dict(row) -> dict:
        value = dict(row)
        for key in ("library_refs", "evidence_refs", "provenance"):
            try:
                value[key] = json.loads(value[key])
            except (TypeError, json.JSONDecodeError):
                value[key] = {} if key == "provenance" else []
        return value

    def model_metadata(self, *, asset: str | None = None, status: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM model_metadata", []
        filters = []
        if asset is not None:
            filters.append("asset=?")
            params.append(asset)
        if status is not None:
            filters.append("status=?")
            params.append(status)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY model_id, model_version"
        return [self._metadata_dict(row) for row in self.db.execute(query, params)]

    def _record_lifecycle_transition(
        self, model_id: str, model_version: str, to_status: str, *, reason: str | None = None
    ):
        if not isinstance(to_status, str) or not to_status:
            raise ValueError("to_status is required")
        with self.transaction():
            metadata = self.get_model_metadata(model_id, model_version)
            if metadata is None:
                raise ValueError("unknown model version")
            now = utcnow().isoformat()
            self.db.execute(
                "UPDATE model_metadata SET status=? WHERE model_id=? AND model_version=?",
                (to_status, model_id, model_version),
            )
            self.db.execute(
                "INSERT INTO lifecycle_transitions(model_id,model_version,from_status,to_status,reason,at) VALUES(?,?,?,?,?,?)",
                (model_id, model_version, metadata["status"], to_status, reason, now),
            )
            return self.get_model_metadata(model_id, model_version)

    def register_recovery(self, kind: str, body: dict, *, resolved: bool = False) -> int:
        if not kind:
            raise ValueError("recovery kind is required")
        with self.transaction():
            now = utcnow().isoformat()
            cur = self.db.execute(
                "INSERT INTO recovery_records(kind,body,created_at,resolved_at) VALUES(?,?,?,?)",
                (kind, dumps(body), now, now if resolved else None),
            )
            return int(cur.lastrowid)

    def recovery_records(self, kind: str | None = None) -> list[dict]:
        query = "SELECT * FROM recovery_records"
        params = []
        if kind is not None:
            query += " WHERE kind=?"
            params.append(kind)
        return [dict(row) for row in self.db.execute(query + " ORDER BY id", params)]

    def register_promotion_receipt(
        self,
        operation_id: str,
        research_run_id: str,
        bundle_hash: str,
        body: dict,
        *,
        status: str = "committed",
    ) -> dict:
        if not operation_id or not research_run_id:
            raise ValueError("promotion receipt identities are required")
        self._hash(bundle_hash, "bundle_hash")
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM promotion_receipts WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is not None:
                if row["bundle_hash"] != bundle_hash or row["research_run_id"] != research_run_id:
                    raise ValueError("promotion receipt identity conflict")
                return dict(row)
            now = utcnow().isoformat()
            self.db.execute(
                "INSERT INTO promotion_receipts(operation_id,research_run_id,bundle_hash,status,body,created_at) VALUES(?,?,?,?,?,?)",
                (operation_id, research_run_id, bundle_hash, status, dumps(body), now),
            )
            return dict(
                self.db.execute(
                    "SELECT * FROM promotion_receipts WHERE operation_id=?", (operation_id,)
                ).fetchone()
            )

    def promotion_receipt(self, operation_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM promotion_receipts WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_model_metadata(self, metadata: dict, *, transition_reason: str | None = None):
        """Idempotently register immutable canonical metadata."""
        required = {
            "model_id",
            "system_id",
            "system_version",
            "model_version",
            "asset",
            "timeframe",
            "status",
            "card_hash",
            "weights_hash",
        }
        missing = required - metadata.keys()
        if missing:
            raise ValueError("missing model metadata: " + ",".join(sorted(missing)))
        provider = metadata.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("model provider identity is required")
        if metadata["status"] not in {"challenger", "eligible", "retired", "rejected"}:
            raise ValueError("active status requires a verified pointer transition")
        reject_legacy_artifact(metadata, kind="registry")
        self._hash(metadata["card_hash"], "card_hash")
        self._hash(metadata["weights_hash"], "weights_hash")
        from .registry.identity import ModelIdentity

        identity = metadata["model_id"], metadata["model_version"]
        ModelIdentity(metadata["system_id"], metadata["system_version"], *identity,
                      metadata["asset"], metadata["timeframe"])
        with self.transaction():
            existing = self.get_model_metadata(*identity)
            immutable = (
                "system_id",
                "system_version",
                "model_version",
                "asset",
                "timeframe",
                "card_hash",
                "weights_hash",
            )
            if existing is not None:
                if any(metadata.get(k) != existing.get(k) for k in immutable):
                    raise ValueError("model metadata is immutable")
                existing_provider = existing.get("provider")
                if existing_provider and existing_provider.strip() and existing_provider != provider:
                    raise ValueError("model provider is immutable")
                if not existing_provider or not existing_provider.strip():
                    self.db.execute(
                        "UPDATE model_metadata SET provider=? WHERE model_id=? AND model_version=?",
                        (provider, *identity),
                    )
                if existing["status"] != metadata["status"]:
                    if existing["status"] == "active":
                        raise ValueError("active lifecycle requires a verified pointer transition")
                    return self._record_lifecycle_transition(
                        *identity, metadata["status"], reason=transition_reason
                    )
                return self.get_model_metadata(*identity)
            now = utcnow().isoformat()
            self.db.execute(
                "INSERT INTO model_metadata(model_id,system_id,system_version,model_version,asset,timeframe,status,card_hash,weights_hash,provider,library_refs,evidence_refs,provenance,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    metadata["model_id"],
                    metadata["system_id"],
                    metadata["system_version"],
                    metadata["model_version"],
                    metadata["asset"],
                    metadata["timeframe"],
                    metadata["status"],
                    metadata["card_hash"],
                    metadata["weights_hash"],
                    provider,
                    dumps(metadata.get("library_refs", [])),
                    dumps(metadata.get("evidence_refs", [])),
                    dumps(metadata.get("provenance", {})),
                    now,
                ),
            )
            self.db.execute(
                "INSERT INTO lifecycle_transitions(model_id,model_version,from_status,to_status,reason,at) VALUES(?,?,?,?,?,?)",
                (*identity, None, metadata["status"], transition_reason, now),
            )
            return self.get_model_metadata(*identity)

    register_model_metadata = upsert_model_metadata
    register_model = upsert_model_metadata

    def _model_transition_blocker(self) -> str | None:
        if self.pending():
            return "intent_pending"
        if self.open_session():
            return "session_open"
        return None

    def _reject_model_transition(self, asset, model_id, operation_id, reason):
        self.event(
            "model_transition_rejected",
            asset=asset,
            model_id=model_id,
            operation_id=operation_id,
            reason=reason,
        )

    def _write_active_pointer(
        self,
        asset,
        model_id,
        model_version,
        commit_hash,
        *,
        metadata,
        current,
        operation_id,
    ):
        previous = (current["model_id"], current["model_version"]) if current else (None, None)
        predecessor = previous if previous != (model_id, model_version) else (None, None)
        now = utcnow().isoformat()
        self.db.execute(
            "INSERT OR REPLACE INTO active_pointers(asset,model_id,model_version,commit_hash,updated_at) "
            "VALUES(?,?,?,?,?)",
            (asset, model_id, model_version, commit_hash, now),
        )
        self.db.execute(
            "INSERT INTO active_pointer_history(asset,model_id,model_version,commit_hash,"
            "predecessor_model_id,predecessor_model_version,operation_id,changed_at) VALUES(?,?,?,?,?,?,?,?)",
            (asset, model_id, model_version, commit_hash, *predecessor, operation_id, now),
        )
        if predecessor[0] is not None:
            self._record_lifecycle_transition(
                *predecessor, "retired", reason=f"replaced_by:{model_id}@{model_version}"
            )
        if metadata["status"] != "active":
            self._record_lifecycle_transition(model_id, model_version, "active", reason="activation")
        return self.active_pointer(asset)

    @staticmethod
    def _verified_activation_card(asset, model_id, metadata, registry, providers):
        if registry is None or providers is None:
            raise ValueError("transition requires artifact and provider verification")
        from .runtime.resolver import verified_model_card

        card = verified_model_card(registry, model_id, asset=asset, metadata=metadata)
        provider = providers.resolve(card)
        if provider is None or getattr(provider, "runtime_key", None) != metadata["provider"]:
            raise ValueError("transition provider is unavailable or unhealthy")
        return card

    def set_active_pointer(
        self,
        asset: str,
        model_id: str,
        model_version: str,
        commit_hash: str,
        *,
        card_hash: str | None = None,
        weights_hash: str | None = None,
        provider: str | None = None,
        predecessor_model_id: str | None = None,
        predecessor_model_version: str | None = None,
        operation_id: str | None = None,
        registry=None,
        provider_resolver=None,
    ):
        """Compare and replace; a null predecessor explicitly expects a vacant pointer."""
        if not asset or not asset.isupper():
            raise ValueError("invalid asset")
        self._hash(commit_hash, "commit_hash")
        with self.transaction():
            blocker = self._model_transition_blocker()
            if not blocker:
                metadata = self.get_model_metadata(model_id, model_version)
                if metadata is None:
                    raise ValueError("unknown model")
                if metadata["asset"] != asset or metadata["model_version"] != model_version:
                    raise ValueError("active pointer identity differs")
                if card_hash is not None and card_hash != metadata["card_hash"]:
                    raise ValueError("card hash differs")
                if weights_hash is not None and weights_hash != metadata["weights_hash"]:
                    raise ValueError("weights hash differs")
                if provider is not None and provider != metadata.get("provider"):
                    raise ValueError("provider identity differs")
                current = self.active_pointer(asset)
                expected = (predecessor_model_id, predecessor_model_version)
                if predecessor_model_id is not None and predecessor_model_version is None:
                    previous = self.get_model_metadata(predecessor_model_id)
                    expected = (predecessor_model_id, previous["model_version"] if previous else None)
                current_identity = (current["model_id"], current["model_version"]) if current else (None, None)
                if expected == current_identity:
                    if metadata["status"] not in {"eligible", "active"}:
                        raise ValueError("activation requires an eligible model")
                    self._verified_activation_card(asset, model_id, metadata, registry, provider_resolver)
                    if current_identity == (model_id, model_version) and current["commit_hash"] == commit_hash:
                        return current
                    return self._write_active_pointer(
                        asset,
                        model_id,
                        model_version,
                        commit_hash,
                        metadata=metadata,
                        current=current,
                        operation_id=operation_id,
                    )
                blocker = "predecessor does not match active pointer"
            self._reject_model_transition(asset, model_id, operation_id, blocker)
        raise ValueError(blocker)

    def activate_model(
        self,
        asset: str,
        model_id: str,
        model_version: str,
        *,
        card_hash: str,
        weights_hash: str,
        provider: str,
        commit_hash: str,
        predecessor_model_id: str | None = None,
        predecessor_model_version: str | None = None,
        operation_id: str | None = None,
        registry=None,
        provider_resolver=None,
    ) -> dict:
        if not provider:
            raise ValueError("provider identity is required")
        return self.set_active_pointer(
            asset,
            model_id,
            model_version,
            commit_hash,
            card_hash=card_hash,
            weights_hash=weights_hash,
            provider=provider,
            predecessor_model_id=predecessor_model_id,
            predecessor_model_version=predecessor_model_version,
            operation_id=operation_id,
            registry=registry, provider_resolver=provider_resolver,
        )

    activate_pointer = activate_model

    def rollback_active(
        self,
        asset: str,
        *,
        predecessor_model_id: str | None = None,
        predecessor_model_version: str | None = None,
        registry=None,
        provider_resolver=None,
        operation_id: str | None = None,
        commit_hash: str | None = None,
        reason: str = "rollback",
    ) -> dict:
        with self.transaction():
            blocker = self._model_transition_blocker()
            if not blocker:
                current = self.active_pointer(asset)
                if current is None:
                    raise ValueError("no active pointer")
                predecessor = self.predecessor(asset, current["model_id"], current["model_version"])
                if predecessor is None or predecessor["status"] != "retired":
                    raise ValueError("no registered retired predecessor")
                if (
                    predecessor_model_id is not None
                    and predecessor["model_id"] != predecessor_model_id
                ) or (
                    predecessor_model_version is not None
                    and predecessor["model_version"] != predecessor_model_version
                ):
                    raise ValueError("rollback predecessor does not match recorded predecessor")
                card = self._verified_activation_card(asset, predecessor["model_id"], predecessor, registry, provider_resolver)
                digest = self._hash(commit_hash or predecessor["card_hash"], "commit_hash")
                result = self._write_active_pointer(
                    asset,
                    predecessor["model_id"],
                    predecessor["model_version"],
                    digest,
                    metadata=predecessor,
                    current=current,
                    operation_id=operation_id or f"rollback:{asset}:{current['model_id']}",
                )
                self.event("model_rolled_back", asset=asset, model_id=card.model_id, reason=reason)
                return result
            self._reject_model_transition(asset, predecessor_model_id, operation_id, blocker)
        raise ValueError(blocker)

    def active_pointer(self, asset: str) -> dict | None:
        row = self.db.execute("SELECT * FROM active_pointers WHERE asset=?", (asset,)).fetchone()
        if row is None:
            return None
        pointer = dict(row)
        metadata = self.get_model_metadata(pointer["model_id"], pointer["model_version"]) or {}
        pointer.update(
            {
                "card_hash": metadata.get("card_hash"),
                "weights_hash": metadata.get("weights_hash"),
                "provider": metadata.get("provider"),
            }
        )
        return pointer

    get_active_pointer = active_pointer

    def predecessor(
        self, asset: str, model_id: str | None = None, model_version: str | None = None
    ) -> dict | None:
        metadata = self.active_pointer(asset) if model_id is None else self.get_model_metadata(
            model_id, model_version
        )
        if not metadata or metadata["asset"] != asset:
            return None
        row = self.db.execute(
            "SELECT predecessor_model_id,predecessor_model_version FROM active_pointer_history "
            "WHERE asset=? AND model_id=? AND model_version=? ORDER BY seq DESC LIMIT 1",
            (asset, metadata["model_id"], metadata["model_version"]),
        ).fetchone()
        return self.get_model_metadata(*row) if row and row[0] else None

    lookup_predecessor = predecessor

    def active_pointers(self):
        return [
            self.active_pointer(row["asset"])
            for row in self.db.execute("SELECT asset FROM active_pointers ORDER BY asset")
        ]

    def record_routing_decision(self, decision: dict):
        required = {
            "asset",
            "decision_day",
            "reason",
            "score_config",
            "gates",
            "scores",
            "evidence_window",
            "identities",
            "status",
            "decision_sha256",
            "created_at",
        }
        if not required <= decision.keys():
            raise ValueError("routing decision is incomplete")
        digest = self._hash(decision["decision_sha256"], "decision_sha256")
        selected = decision.get("selected_model_id")
        identities = decision["identities"]
        if selected:
            identity = identities.get(selected, {})
            for key in ("card_sha256", "weights_sha256", "snapshot_sha256"):
                self._hash(identity.get(key), key)
            if (
                not identity.get("provider")
                or decision["gates"].get(selected, {}).get("card_hash") is not True
            ):
                raise ValueError("selected model lacks verified provider or card")
        with self.transaction():
            existing = self.routing_decision(decision["asset"], decision["decision_day"])
            if existing is not None:
                if existing.get("decision_sha256") == digest:
                    return
                raise ValueError("routing decision already recorded for asset and day")
            cursor = self.db.execute(
                "INSERT INTO routing_decisions(asset,decision_day,selected_model_id,reason,score_config,"
                "gates,scores,evidence_window,card_hashes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    decision["asset"],
                    decision["decision_day"],
                    selected,
                    decision["reason"],
                    dumps(decision["score_config"]),
                    dumps(decision["gates"]),
                    dumps(decision["scores"]),
                    dumps(decision["evidence_window"]),
                    dumps({key: item["card_sha256"] for key, item in identities.items()}),
                    decision["created_at"],
                ),
            )
            self.db.execute(
                "INSERT INTO routing_decision_details VALUES(?,?,?,?,?)",
                (
                    cursor.lastrowid,
                    digest,
                    dumps(identities),
                    decision["status"],
                    decision.get("system_id"),
                ),
            )

    def routing_decision(self, asset: str, day: str) -> dict | None:
        row = self.db.execute(
            "SELECT r.*, d.decision_sha256, d.identities, d.status, d.system_id "
            "FROM routing_decisions r LEFT JOIN routing_decision_details d ON d.routing_id=r.id "
            "WHERE r.asset=? AND r.decision_day=?",
            (asset, day),
        ).fetchone()
        if row is None:
            return None
        decision = dict(row)
        for key in ("score_config", "gates", "scores", "evidence_window", "identities"):
            if decision[key] is not None:
                decision[key] = json.loads(decision[key])
        # Historical card_hashes were mislabeled and cannot authorize operation.
        decision.pop("card_hashes")
        return decision

    @staticmethod
    def restore(row):
        values = json.loads(row["body"])
        for name in (
            "initial_bank",
            "stake",
            "bank",
            "net",
            "losses",
            "flat_stake",
            "stop_loss",
            "stop_win",
            "original_stop_loss",
            "original_stop_win",
            "starting_day_net",
        ):
            if values.get(name) is not None:
                values[name] = Decimal(values[name])
        return Session(**values)


    @staticmethod
    def _drill_hash(value: str, label: str) -> str:
        return Store._hash(value, label)

    def _decode_drill(self, row):
        if row is None:
            return None
        value = dict(row)
        value["context"] = json.loads(value["context"])
        return value

    def test_drill(self, drill_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM test_drills WHERE id=?", (drill_id,)).fetchone()
        return self._decode_drill(row)

    def create_test_drill(
        self,
        *,
        asset: str,
        model_id: str,
        model_version: str,
        card_hash: str,
        weights_hash: str,
        provider: str,
        account_binding: str,
        context: dict,
        fingerprint: str,
        actor: str,
        created_at,
        expires_at,
        drill_id: str | None = None,
    ) -> dict:
        if not asset or not asset.isupper() or not asset.isalnum():
            raise ValueError("drill asset is invalid")
        if not all(isinstance(value, str) and value for value in (
            model_id, model_version, provider, account_binding, fingerprint, actor,
        )):
            raise ValueError("drill identity is incomplete")
        self._hash(card_hash, "card_hash")
        self._hash(weights_hash, "weights_hash")
        self._hash(fingerprint, "fingerprint")
        if created_at.utcoffset() is None or expires_at.utcoffset() is None or expires_at <= created_at:
            raise ValueError("drill expiry must be a future timezone-aware instant")
        if (expires_at - created_at).total_seconds() > 15 * 60:
            raise ValueError("drill authorization cannot exceed 15 minutes")
        with self.transaction():
            if self.open_session() or self.pending():
                raise ValueError("test-drill plan requires no open session or pending intent")
            for row in self.db.execute(
                "SELECT id FROM test_drills WHERE asset=? AND status IN ('PLANNED','AUTHORIZED') AND expires_at<=?",
                (asset, created_at.isoformat()),
            ).fetchall():
                self.expire_test_drill(row["id"], reason="authorization_elapsed")
            existing = self.db.execute(
                "SELECT id FROM test_drills WHERE asset=? AND status IN ('PLANNED','AUTHORIZED','CONSUMED','PENDING','UNKNOWN')",
                (asset,),
            ).fetchone()
            if existing:
                raise ValueError("another test drill is active for this asset")
            identity = drill_id or str(uuid.uuid4())
            now = created_at.isoformat()
            self.db.execute(
                "INSERT INTO test_drills(id,asset,model_id,model_version,card_hash,weights_hash,provider,account_binding,context,fingerprint,actor,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, asset, model_id, model_version, card_hash, weights_hash, provider,
                 account_binding, dumps(context), fingerprint, actor, now, expires_at.isoformat(), "PLANNED"),
            )
            self.event("test_drill_planned", drill_id=identity, asset=asset, model_id=model_id, actor=actor,
                       expires_at=expires_at.isoformat())
            return self.test_drill(identity)

    def authorize_test_drill(self, drill_id: str, *, fingerprint: str, actor: str, now) -> dict:
        self._hash(fingerprint, "fingerprint")
        with self.transaction():
            drill = self.test_drill(drill_id)
            if drill is None:
                raise ValueError("unknown test drill")
            if drill["status"] != "PLANNED":
                raise ValueError("test drill is not awaiting authorization")
            if drill["fingerprint"] != fingerprint or drill["actor"] != actor:
                raise ValueError("test drill authorization identity differs")
            expires = datetime.fromisoformat(drill["expires_at"])
            if now.utcoffset() is None or now > expires:
                self.db.execute("UPDATE test_drills SET status='EXPIRED' WHERE id=?", (drill_id,))
                self.event("test_drill_expired", drill_id=drill_id, phase="authorize")
                raise ValueError("test drill authorization expired")
            self.db.execute(
                "UPDATE test_drills SET status='AUTHORIZED',confirmed_at=? WHERE id=?",
                (now.isoformat(), drill_id),
            )
            self.event("test_drill_authorized", drill_id=drill_id, actor=actor)
            return self.test_drill(drill_id)

    def _consume_test_drill_locked(self, drill_id: str, signal, model_pointer, account_binding, now):
        drill = self.test_drill(drill_id)
        if drill is None:
            raise ValueError("unknown test drill")
        if drill["status"] != "AUTHORIZED":
            raise ValueError("test drill authorization is not available")
        expires = datetime.fromisoformat(drill["expires_at"])
        if now.utcoffset() is None or now > expires:
            self.db.execute("UPDATE test_drills SET status='EXPIRED' WHERE id=?", (drill_id,))
            self.event("test_drill_expired", drill_id=drill_id, phase="consume")
            raise ValueError("test drill authorization expired")
        if drill["account_binding"] != account_binding or drill["asset"] != signal.symbol:
            raise ValueError("test drill context differs")
        if not isinstance(model_pointer, dict) or any(
            drill[key] != model_pointer.get(key)
            for key in ("asset", "model_id", "model_version", "card_hash", "weights_hash", "provider")
        ):
            raise ValueError("test drill model context differs")
        return drill

    def _link_test_drill_intent_locked(self, drill_id: str, intent_id: str):
        self.db.execute(
            "INSERT INTO test_drill_intents(drill_id,intent_id) VALUES(?,?)", (drill_id, intent_id)
        )
        self.db.execute(
            "UPDATE test_drills SET status='PENDING',consumed_at=?,intent_id=? WHERE id=?",
            (utcnow().isoformat(), intent_id, drill_id),
        )
        self.event("test_drill_authorization_consumed", drill_id=drill_id, intent=intent_id)

    def expire_test_drill(self, drill_id: str, *, reason: str = "expired") -> dict | None:
        with self.transaction():
            drill = self.test_drill(drill_id)
            if drill and drill["status"] in {"PLANNED", "AUTHORIZED"}:
                self.db.execute("UPDATE test_drills SET status='EXPIRED',result=? WHERE id=?", (reason, drill_id))
                self.event("test_drill_expired", drill_id=drill_id, phase=reason)
            return self.test_drill(drill_id)

    def mark_test_drill_unknown(self, drill_id: str, *, reason: str):
        with self.transaction():
            drill = self.test_drill(drill_id)
            if drill and drill["status"] in {"PENDING", "CONSUMED", "UNKNOWN"}:
                self.db.execute("UPDATE test_drills SET status='UNKNOWN',result=? WHERE id=?", (reason, drill_id))
                self.event("test_drill_unknown", drill_id=drill_id, reason=reason)

    def _finalize_test_drill_locked(self, intent_id: str, result: str):
        row = self.db.execute(
            "SELECT d.* FROM test_drills d JOIN test_drill_intents x ON x.drill_id=d.id WHERE x.intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None or result in {"UNKNOWN", "OPEN"}:
            return None
        drill = dict(row)
        if drill["status"] in {"SETTLED", "NOT_SENT", "CANCELED", "EXPIRED"}:
            return self._decode_drill(row)
        self.db.execute(
            "UPDATE test_drills SET status=?,result=? WHERE id=?",
            ("NOT_SENT" if result == "NOT_SENT" else "CANCELED" if result == "CANCELED" else "SETTLED", result, drill["id"]),
        )
        session = self.open_session()
        if session and session["id"] == self.db.execute("SELECT session_id FROM intents WHERE id=?", (intent_id,)).fetchone()[0]:
            body = self.restore(session)
            body.complete = True
            body.close_reason = "test_drill_" + result.lower()
            self.db.execute("UPDATE sessions SET body=?,complete=1 WHERE id=?", (dumps(asdict(body)), session["id"]))
        self.event("test_drill_session_closed", drill_id=drill["id"], intent=intent_id, result=result)
        return self.test_drill(drill["id"])

    def finalize_test_drill(self, intent_id: str, result: str):
        with self.transaction():
            return self._finalize_test_drill_locked(intent_id, result)

    def test_receipt(self, drill_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM test_receipts WHERE drill_id=?", (drill_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["body"] = json.loads(value["body"])
        return value

    def publish_test_receipt(self, *, drill_id: str, intent_id: str, body: dict, created_at, expires_at) -> dict:
        if expires_at <= created_at or created_at.utcoffset() is None or expires_at.utcoffset() is None:
            raise ValueError("receipt validity must be timezone-aware and positive")
        with self.transaction():
            drill = self.test_drill(drill_id)
            if drill is None or drill["intent_id"] != intent_id:
                raise ValueError("test drill is not settled")
            existing = self.test_receipt(drill_id)
            if existing:
                if existing["intent_id"] != intent_id or existing["body"] != body:
                    raise ValueError("test receipt identity conflict")
                return existing
            if drill["status"] != "SETTLED":
                raise ValueError("test drill is not settled")
            receipt_id = hashlib.sha256(f"{drill_id}|{intent_id}".encode()).hexdigest()
            self.db.execute(
                "INSERT INTO test_receipts(id,drill_id,intent_id,body,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (receipt_id, drill_id, intent_id, dumps(body), created_at.isoformat(), expires_at.isoformat()),
            )
            self.db.execute("UPDATE test_drills SET status='RECEIPTED',receipt_id=? WHERE id=?", (receipt_id, drill_id))
            self.event("test_drill_receipt_published", drill_id=drill_id, intent=intent_id, receipt_id=receipt_id)
            return self.test_receipt(drill_id)

    def pending(self):
        row = self.db.execute(
            "SELECT * FROM intents WHERE state IN ('DISPATCHING','ACCEPTED','UNKNOWN')"
        ).fetchone()
        return dict(row) if row else None

    def matches_signal_pointer(self, signal, pointer) -> bool:
        if not isinstance(pointer, dict) or any(
            not pointer.get(field)
            for field in (
                "asset",
                "model_id",
                "model_version",
                "card_hash",
                "weights_hash",
                "provider",
                "commit_hash",
            )
        ):
            return False
        if (pointer["asset"], pointer["model_id"], pointer["card_hash"]) != (
            signal.symbol,
            signal.model_id,
            signal.card_sha256,
        ):
            return False
        return self.active_pointer(signal.symbol) == pointer

    def reserve(self, signal, session, *, model_pointer=None, drill_id=None, account_binding=None, now=None, broker_symbol=None):
        identity = hashlib.sha256(
            f"{signal.symbol}|{signal.candle_at.isoformat()}|{signal.direction}".encode()
        ).hexdigest()
        with self.transaction():
            if (
                self.get("stop_requested", False)
                or self.pending()
                or self.db.execute("SELECT 1 FROM intents WHERE id=?", (identity,)).fetchone()
            ):
                return None
            current = self.open_session()
            if not current or current["id"] != session["id"]:
                raise ValueError("Sessão não está ativa")
            s = self.restore(current)
            if s.demo_continuous:
                raise SessionUnavailable("Sessão demo legada só admite reconciliação")
            has_model = signal.model_id is not None or signal.card_sha256 is not None
            if has_model or model_pointer is not None:
                if not model_pointer or not self.matches_signal_pointer(signal, model_pointer):
                    self.event("model_identity_changed", symbol=signal.symbol, phase="reserve")
                    return None
            if s.session_day is not None:
                day = operational_day(
                    signal.entry_at, s.session_timezone, s.session_start, s.session_end
                )
                if day != s.session_day or s.bounded_stake() != s.stake:
                    raise SessionUnavailable("Reserva incompatível com o dia ou limite operacional")
            if drill_id is not None:
                self._consume_test_drill_locked(
                    drill_id, signal, model_pointer, account_binding, now or utcnow()
                )
            context = {**asdict(signal), "version": s.version}
            if broker_symbol is not None:
                context["broker_symbol"] = broker_symbol
            if context.pop("recipe_id", None) is not None:
                raise ValueError("legacy signal identity cannot be reserved")
            if model_pointer is not None:
                context["model_identity"] = model_pointer
            self.db.execute(
                "INSERT INTO intents VALUES(?,?,?,?,?,?,?,'DISPATCHING',NULL,NULL,NULL,NULL,?)",
                (
                    identity,
                    s_id := current["id"],
                    signal.symbol,
                    signal.direction,
                    signal.candle_at.isoformat(),
                    signal.entry_at.isoformat(),
                    str(s.stake),
                    dumps(context),
                ),
            )
            if drill_id is not None:
                self._link_test_drill_intent_locked(drill_id, identity)
            self.event("signal_selected", intent=identity, session=s_id, **context)
        return identity

    def mark_unknown(self, identity):
        self.db.execute(
            "UPDATE intents SET state='UNKNOWN' WHERE id=? AND state IN "
            "('DISPATCHING','ACCEPTED','UNKNOWN')",
            (identity,),
        )

    def remember_client_order(self, identity, client_id):
        self.db.execute(
            "UPDATE intents SET state='UNKNOWN',client_id=? WHERE id=? "
            "AND state IN ('DISPATCHING','UNKNOWN') AND broker_id IS NULL "
            "AND (client_id IS NULL OR client_id=?)",
            (client_id, identity, client_id),
        )

    def accept(self, identity, broker_id, client_id):
        self.db.execute(
            "UPDATE intents SET state='ACCEPTED',broker_id=?,client_id=? "
            "WHERE id=? AND state IN ('DISPATCHING','ACCEPTED','UNKNOWN')",
            (broker_id, client_id, identity),
        )

    def settle(self, identity, status, profit: Decimal):
        canonical_status = normalize_outcome(status, source="broker")
        with self.transaction():
            row = self.db.execute("SELECT * FROM intents WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Intenção inexistente")
            if row["state"] not in {"DISPATCHING", "ACCEPTED", "UNKNOWN"}:
                return
            current = self.open_session()
            if not current or current["id"] != row["session_id"]:
                raise ValueError("Sessão da intenção não encontrada")
            session = self.restore(current)
            if status in {"CANCELED", "NOT_SENT"}:
                net = Decimal(0)
            else:
                net = session.resolve(canonical_status, profit)
            # Old demo orders closed their session on any terminal result.
            if session.demo_continuous:
                session.complete, session.close_reason = True, "demo_" + canonical_status.lower()
            self.db.execute(
                "UPDATE sessions SET body=?,complete=? WHERE id=?",
                (dumps(asdict(session)), int(session.complete), current["id"]),
            )
            self.db.execute(
                "UPDATE intents SET state=?,profit=?,net=? WHERE id=?",
                (canonical_status, str(profit), str(net), identity),
            )
            if session.complete and session.close_reason in {
                "stop_loss",
                "stop_win",
                "capital_floor",
            }:
                day = session.session_day
                daily_losses, daily_net = self._calendar_day_results(day)
                loss_limit = session.original_stop_loss or session.stop_loss
                win_limit = session.original_stop_win or session.stop_win
                daily_loss_hit = (
                    session.stop_loss_scope == "calendar_day"
                    and loss_limit is not None
                    and loss_limit - daily_losses < 1
                )
                daily_win_hit = (
                    session.stop_when_limit_reached
                    and session.stop_win_scope == "calendar_day"
                    and win_limit is not None
                    and daily_net >= win_limit
                )
                reason = (
                    "stop_loss"
                    if daily_loss_hit
                    else "stop_win"
                    if daily_win_hit
                    else session.close_reason
                )
                policy = {
                    "stop_loss": session.after_stop_loss,
                    "stop_win": session.after_stop_win,
                }.get(reason)
                if day and (
                    daily_loss_hit or daily_win_hit or policy in {"next_day", "manual_reset"}
                ):
                    lock = {"day": day, "reason": reason}
                    if policy == "manual_reset":
                        lock["policy"] = policy
                    self.set("operation_lock", lock)
            self.event(
                "strategy_result"
                if canonical_status in {"WIN", "LOSS", "TIE"}
                else "technical_result",
                intent=identity,
                status=canonical_status,
                net=str(net),
            )
            self._finalize_test_drill_locked(identity, canonical_status)

    def study(self, identity, body):
        self.db.execute(
            "INSERT INTO studies VALUES(?,?,?)", (identity, utcnow().isoformat(), dumps(body))
        )

    def report(self):
        counts = {
            r[0]: r[1] for r in self.db.execute("SELECT state,count(*) FROM intents GROUP BY state")
        }
        net = sum(
            (
                Decimal(r[0])
                for r in self.db.execute("SELECT net FROM intents WHERE net IS NOT NULL")
            ),
            Decimal(0),
        )
        latest_drill = self.db.execute(
            "SELECT id FROM test_drills ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(
            counts=counts,
            net=str(net),
            completed_sessions=self.db.execute(
                "SELECT count(*) FROM sessions WHERE complete=1"
            ).fetchone()[0],
            pending=self.pending(),
            active_version=self.get("active"),
            pilot=self.get("pilot"),
            test_drill=self.test_drill(latest_drill[0]) if latest_drill else None,
        )
