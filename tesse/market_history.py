"""Durable Ebinex research jobs. This queue never opens the operational Store."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

SOURCE = "ebinex:TEST:OPTION:M1"
DAY = 86_400
QUEUE_BUSY_SECONDS = 1
RUNNING_TTL = 120  # diagnostic only; the worker lock is the ownership authority
ACTIVE = "('queued','running','waiting')"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, identity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', binding TEXT, token TEXT,
    cursor INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0, claimed_at REAL, reason TEXT, report TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_identity ON jobs(identity) WHERE state IN {ACTIVE};
CREATE TABLE IF NOT EXISTS candles (
    job_id TEXT NOT NULL REFERENCES jobs(id), symbol TEXT NOT NULL,
    minute INTEGER NOT NULL, ohlcv TEXT NOT NULL, PRIMARY KEY (job_id,symbol,minute)
);
CREATE TABLE IF NOT EXISTS evidence (
    job_id TEXT NOT NULL REFERENCES jobs(id), page INTEGER NOT NULL,
    detail TEXT NOT NULL, PRIMARY KEY (job_id,page)
);
"""


@dataclass(frozen=True, slots=True)
class Request:
    kind: str
    symbols: tuple[str, ...]
    start: int | None
    end: int

    def __post_init__(self):
        if not self.symbols or any(not re.fullmatch(r"[A-Z0-9]{1,32}", s) for s in self.symbols):
            raise ValueError("Informe símbolos nativos Ebinex em maiúsculas")
        object.__setattr__(self, "symbols", tuple(sorted(set(self.symbols))))
        if type(self.end) is not int or self.end <= 0 or self.end % DAY:
            raise ValueError("Use datas completas UTC")
        if self.kind == "sync" and type(self.start) is int and 0 <= self.start < self.end:
            if self.start % DAY == 0:
                return
        if self.kind == "probe" and self.start is None and self.end >= 90 * DAY:
            return
        raise ValueError("Intervalo UTC inválido para histórico Ebinex")

    @property
    def identity(self):
        return json.dumps({"source": SOURCE, **asdict(self)}, sort_keys=True)

    @classmethod
    def from_identity(cls, identity):
        payload = json.loads(identity)
        if payload.pop("source") != SOURCE:
            raise ValueError("Fonte inválida para coleta Ebinex")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    request: Request
    cursor: int
    attempts: int
    token: str


class HistoryQueue:
    def __init__(self, root):
        self.path = Path(root) / "market-data" / "ebinex" / "queue.sqlite"

    def _connect(self, *, create=False, read_only=False):
        mode = "ro" if read_only else "rwc" if create else "rw"
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode={mode}", uri=True, timeout=QUEUE_BUSY_SECONDS)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _enable_wal(db):
        # The first connections may contend while SQLite switches journal
        # modes. That upgrade can return BUSY without honoring busy_timeout.
        deadline = time.monotonic() + QUEUE_BUSY_SECONDS
        while True:
            try:
                db.execute("PRAGMA journal_mode=WAL").fetchone()
                return
            except sqlite3.OperationalError as exc:
                remaining = deadline - time.monotonic()
                if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))

    @contextmanager
    def _write(self, *, create=False):
        with closing(self._connect(create=create)) as db, db:
            if create:
                self._enable_wal(db)
                db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            yield db

    def enqueue(self, request: Request):
        identity = request.identity
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._write(create=True) as db:
            row = db.execute(
                f"SELECT id FROM jobs WHERE identity=? AND state IN {ACTIVE}", (identity,)
            ).fetchone()
            if row:
                return row["id"]
            job_id = uuid4().hex
            db.execute("INSERT INTO jobs(id,identity) VALUES(?,?)", (job_id, identity))
            return job_id

    def claim(self, binding: str, *, now: float):
        if not binding or not self.path.is_file():
            return None
        with self._write() as db:
            row = db.execute(
                f"SELECT * FROM jobs WHERE state IN {ACTIVE} ORDER BY seq LIMIT 1"
            ).fetchone()
            if not row:
                return None
            if row["binding"] not in (None, binding):
                db.execute(
                    "UPDATE jobs SET state='failed',reason='account_changed',token=NULL WHERE id=?",
                    (row["id"],),
                )
                return None
            if row["retry_at"] > now:
                return None
            token = uuid4().hex
            db.execute(
                "UPDATE jobs SET binding=?,token=?,state='running',claimed_at=?,reason=NULL WHERE id=?",
                (binding, token, now, row["id"]),
            )
            return Claim(
                row["id"],
                Request.from_identity(row["identity"]),
                row["cursor"],
                row["attempts"],
                token,
            )

    @staticmethod
    def _owns(db, claim):
        return (
            db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND token=? AND cursor=? AND state='running'",
                (claim.id, claim.token, claim.cursor),
            ).fetchone()
            is not None
        )

    def record(self, claim, symbol, rows, evidence, *, retry_at=None):
        if symbol not in claim.request.symbols:
            raise ValueError("Símbolo não pertence ao pedido Ebinex")
        with self._write() as db:
            if not self._owns(db, claim):
                return False
            previous = db.execute(
                "SELECT detail FROM evidence WHERE job_id=? AND page=?", (claim.id, claim.cursor)
            ).fetchone()
            detail = dict(evidence)
            conflicts = set(detail.get("conflicts", ()))
            prior = json.loads(previous["detail"]) if previous else {}
            conflicts.update(prior.get("conflicts", ()))
            duplicates = 0
            for minute, values in rows.items():
                old = db.execute(
                    "SELECT ohlcv FROM candles WHERE job_id=? AND symbol=? AND minute=?",
                    (claim.id, symbol, minute),
                ).fetchone()
                if old:
                    if tuple(map(Decimal, json.loads(old["ohlcv"]))) != tuple(map(Decimal, values)):
                        conflicts.add(minute)
                    else:
                        duplicates += 1
                else:
                    db.execute(
                        "INSERT INTO candles VALUES(?,?,?,?)",
                        (claim.id, symbol, minute, json.dumps(values)),
                    )
            detail.update(
                symbol=symbol,
                attempts=claim.attempts + 1,
                conflicts=sorted(conflicts),
                identical_overlap=prior.get("identical_overlap", 0) + duplicates,
                identical_in_response=(
                    prior.get("identical_in_response", 0) + detail.get("identical_in_response", 0)
                ),
            )
            if conflicts:
                detail.update(quality="inconclusive", reason="conflicting_candles")
                retry_at = None
            detail["attempt_log"] = [
                *prior.get("attempt_log", ()),
                {
                    key: detail[key]
                    for key in ("quality", "reason", "received_minutes")
                    if key in detail
                },
            ]
            db.execute(
                "INSERT OR REPLACE INTO evidence VALUES(?,?,?)",
                (claim.id, claim.cursor, json.dumps(detail)),
            )
            if retry_at is None:
                db.execute(
                    """UPDATE jobs SET cursor=cursor+1, attempts=0, retry_at=0,
                               state='queued', token=NULL, reason=NULL WHERE id=?""",
                    (claim.id,),
                )
            else:
                db.execute(
                    """UPDATE jobs SET attempts=attempts+1, retry_at=?, state='waiting',
                               token=NULL, reason=? WHERE id=?""",
                    (retry_at, detail.get("reason", detail["quality"]), claim.id),
                )
            return True

    def defer(self, claim, reason, *, retry_at=0):
        with self._write() as db:
            if not self._owns(db, claim):
                return False
            db.execute(
                "UPDATE jobs SET state='waiting',token=NULL,reason=?,retry_at=? WHERE id=?",
                (reason, retry_at, claim.id),
            )
            return True

    def finish(self, claim, report):
        with self._write() as db:
            if not self._owns(db, claim):
                return False
            db.execute(
                "UPDATE jobs SET state='completed',token=NULL,report=? WHERE id=?",
                (json.dumps(report), claim.id),
            )
            return True

    def read_rows(self, job_id, symbol):
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute(
                """SELECT minute,ohlcv FROM candles WHERE job_id=? AND symbol=?
                                 ORDER BY minute""",
                (job_id, symbol),
            )
            return [(row["minute"], tuple(json.loads(row["ohlcv"]))) for row in rows]

    def evidence(self, job_id):
        with closing(self._connect(read_only=True)) as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT detail FROM evidence WHERE job_id=? ORDER BY page", (job_id,)
                )
            ]

    def status(self, job_id, *, now=None):
        if not self.path.is_file():
            return None
        with closing(self._connect(read_only=True)) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            latest = db.execute(
                "SELECT detail FROM evidence WHERE job_id=? ORDER BY page DESC LIMIT 1", (job_id,)
            ).fetchone()
        state, reason = row["state"], row["reason"]
        now = time.time() if now is None else now
        if state == "queued" or (state == "running" and now - row["claimed_at"] > RUNNING_TTL):
            state, reason = "waiting", "awaiting_worker"
        return dict(
            id=row["id"],
            **json.loads(row["identity"]),
            state=state,
            reason=reason,
            confirmed_pages=row["cursor"],
            attempts=row["attempts"],
            last_page=json.loads(latest[0]) if latest else None,
            report=json.loads(row["report"]) if row["report"] else None,
        )
