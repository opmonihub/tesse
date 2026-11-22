"""Durable custody of a managed experiment under the active proposal lease.

The state records authorizations and exact run identities; only a supplied run
verifier can attest publication, and only a caller-validated review is stored.
Neither an absent run nor a failed verification establishes a safe retry.
"""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from re import fullmatch
from stat import S_ISDIR, S_ISREG
from typing import Any

from .contracts import Invalid, Workspace, keys, slug
from .layout import project_directory, require_directory, validate_run_id
from .partitions import write_durable_json

STATE_DIRECTORY = 'study-state'
SCHEMA_VERSION = 1
STANDALONE = 'standalone'
PENDING = frozenset({'dispatching', 'published'})
STATUSES = PENDING | {'reviewed', 'prepublication_failed'}


@dataclass(frozen=True)
class AdvanceAuthorization:
    round_id: str
    declaration_hash: str
    adjustment_hash: str | None = None


@dataclass(frozen=True)
class RoundRecord:
    owner: str
    round_id: str
    declaration_hash: str
    run_id: str
    predecessor_run_id: str | None = None
    adjustment_hash: str | None = None
    effective_hash: str | None = None
    status: str = 'dispatching'
    evidence_sha256: str | None = None
    review: dict[str, Any] | None = None
    advance: AdvanceAuthorization | None = None
    retry_of: str | None = None
    intent_hash: str | None = None


@dataclass(frozen=True)
class VerifiedPublication:
    proposal: str
    experiment: str
    phase: str
    owner: str
    round_id: str
    declaration_hash: str
    run_id: str
    predecessor_run_id: str | None
    adjustment_hash: str | None
    evidence_sha256: str
    effective_hash: str | None = None
    retry_of: str | None = None
    intent_hash: str | None = None


@dataclass(frozen=True)
class _State:
    owner: str
    revision: int = 1
    closed: bool = False
    records: tuple[RoundRecord, ...] = ()


@dataclass(frozen=True)
class StudyStateSnapshot:
    """A single revision for inspection; holds no proposal lease or write access."""

    path: Path
    owner: str | None = None
    revision: int = 0
    closed: bool = False
    records: tuple[RoundRecord, ...] = ()

    @property
    def exists(self):
        return self.owner is not None

    @property
    def latest(self):
        return self.records[-1] if self.records else None

    @property
    def authorization(self):
        if not self.closed:
            for record in reversed(self.records):
                if record.status != 'prepublication_failed':
                    return record.advance
        return None

    def get(self, round_id: str):
        slug(round_id, 'study.round_id')
        return next((record for record in self.records if record.round_id == round_id), None)


@dataclass(frozen=True)
class _ReadOnlyProject:
    workspace: Workspace
    proposal_id: str

    def active_directory(self):
        return require_directory(project_directory(self.workspace.root, self.proposal_id))


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('campos JSON duplicados')
    return result


def _check_digest(value, field, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not fullmatch(r'[0-9a-f]{64}', value):
        raise Invalid(field, 'esperado SHA-256 em hexadecimal')


def _check_owner(value):
    if value == STANDALONE:
        return
    slug(value, 'study.owner')
    if not value.startswith('iter-'):
        raise Invalid('study.owner', 'esperado ID de iteração ou standalone')


def _check_round(record):
    _check_owner(record.owner)
    slug(record.round_id, 'study.round_id')
    _check_digest(record.declaration_hash, 'study.declaration_hash')
    validate_run_id(record.run_id)
    if record.predecessor_run_id is not None:
        validate_run_id(record.predecessor_run_id)
    _check_digest(record.adjustment_hash, 'study.adjustment_hash', optional=True)
    _check_digest(record.effective_hash, 'study.effective_hash', optional=True)
    _check_digest(record.intent_hash, 'study.intent_hash', optional=True)
    if record.retry_of is not None:
        slug(record.retry_of, 'study.retry_of')
    if record.status not in STATUSES:
        raise ValueError(f'estado de rodada desconhecido: {record.run_id}')
    if record.status in ('reviewed', 'published'):
        _check_digest(record.evidence_sha256, 'study.evidence_sha256')
    elif record.evidence_sha256 is not None:
        raise ValueError(f'hash de publicação em tentativa não publicada: {record.run_id}')
    if record.status == 'reviewed':
        if type(record.review) is not dict or not record.review:
            raise ValueError(f'revisão inválida: {record.run_id}')
        if record.advance is not None:
            _check_advance(record.advance)
    elif record.review is not None or record.advance is not None:
        raise ValueError(f'revisão sem estado revisado: {record.run_id}')


def _check_advance(advance):
    if not isinstance(advance, AdvanceAuthorization):
        raise Invalid('study.advance', 'esperada autorização estruturada de avanço')
    slug(advance.round_id, 'study.advance.round_id')
    _check_digest(advance.declaration_hash, 'study.advance.declaration_hash')
    _check_digest(advance.adjustment_hash, 'study.advance.adjustment_hash', optional=True)


def _permits(predecessor, record, previous=None):
    if record.retry_of is not None:
        return (previous is not None and previous.status == 'prepublication_failed'
                and previous.round_id == record.retry_of and record.intent_hash is not None
                and (record.intent_hash, record.predecessor_run_id, record.adjustment_hash, record.effective_hash)
                == (previous.intent_hash, previous.predecessor_run_id, previous.adjustment_hash, previous.effective_hash))
    if predecessor is None:
        return record.predecessor_run_id is None and record.adjustment_hash is None
    return (record.predecessor_run_id == predecessor.run_id
            and predecessor.advance == AdvanceAuthorization(
                record.round_id, record.declaration_hash, record.adjustment_hash))


class StudyState:
    """Proposal-local state for one experiment; keep the lease through transitions.

    Reads reload durable state (even two handles with the same live lease cannot
    blindly overwrite one another). `begin` is the sole dispatch checkpoint:
    only `created=True` permits work; an existing attempt must be reconciled.
    The caller must enforce domain-specific review, protocol and budget checks
    before recording a review or beginning a new dispatch.
    """

    def __init__(self, lease, experiment_id: str):
        self._lease = lease
        self._experiment = slug(experiment_id, 'study.experiment')
        self._new_attempts: set[str] = set()

    @classmethod
    def read_only(cls, workspace: Workspace, proposal_id: str, experiment_id: str):
        """Read an atomic snapshot under the caller's workspace layout guard."""
        return cls(_ReadOnlyProject(workspace, proposal_id), experiment_id).snapshot()

    @property
    def path(self):
        return self._lease.active_directory() / STATE_DIRECTORY / f'{self._experiment}.json'

    def _error(self, message):
        return Invalid(f'study.state.{self._experiment}', message)

    def _identity(self):
        return {
            'schema_version': SCHEMA_VERSION,
            'workspace': str(self._lease.workspace.root.resolve()),
            'proposal': self._lease.proposal_id,
            'experiment': self._experiment,
        }

    def _load(self):
        path = self.path
        try:
            require_directory(path.parent, missing_ok=True)
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                return None
            if not S_ISREG(mode):
                raise ValueError('esperado arquivo regular sem symlink')
            raw = json.loads(path.read_bytes(), object_pairs_hook=_unique_keys)
            identity = self._identity()
            keys(raw, 'study.state', required=(*identity, 'owner', 'revision', 'closed',
                                              'records', 'state_sha256'))
            if raw.pop('state_sha256') != _digest(raw):
                raise ValueError('SHA-256 do estado não confere')
            if type(raw['schema_version']) is not int or any(
                raw[name] != value for name, value in identity.items()
            ):
                raise ValueError('versão ou identidade do estado não confere')
            _check_owner(raw['owner'])
            if type(raw['revision']) is not int or type(raw['closed']) is not bool or type(raw['records']) is not list:
                raise ValueError('revisão, encerramento ou rodadas inválidas')
            records = self._decode_records(raw)
            revision = 1 + raw['closed'] + sum(
                {'dispatching': 1, 'published': 2, 'reviewed': 3, 'prepublication_failed': 2}[record.status]
                for record in records
            )
            if raw['revision'] != revision:
                raise ValueError('revisão do estado não corresponde às transições')
            return _State(raw['owner'], revision, raw['closed'], records)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise self._error(f'estado não verificável: {exc}') from exc

    @staticmethod
    def _decode_records(raw):
        records = []
        seen_rounds, seen_runs = set(), set()
        predecessor = None
        for index, row in enumerate(raw['records']):
            keys(row, 'study.state.round', required=RoundRecord.__dataclass_fields__)
            advance = row['advance']
            if advance is not None:
                keys(advance, 'study.state.advance', required=AdvanceAuthorization.__dataclass_fields__)
                advance = AdvanceAuthorization(**advance)
            record = RoundRecord(**(row | {'advance': advance}))
            _check_round(record)
            if record.owner != raw['owner'] or record.round_id in seen_rounds or record.run_id in seen_runs:
                raise ValueError(f'identidade duplicada ou fora do dono: {record.run_id}')
            if not _permits(predecessor, record, records[-1] if records else None):
                raise ValueError(f'predecessor ou autorização incompatível: {record.run_id}')
            if record.status in PENDING and (raw['closed'] or index != len(raw['records']) - 1):
                raise ValueError(f'rodada pendente seguida por avanço: {record.run_id}')
            seen_rounds.add(record.round_id)
            seen_runs.add(record.run_id)
            if record.advance is not None and record.advance.round_id in seen_rounds:
                raise ValueError(f'autorização reutiliza ID de rodada: {record.run_id}')
            if record.status == 'reviewed':
                predecessor = record
            records.append(record)
        return tuple(records)

    def _write(self, state):
        path = self.path
        try:
            require_directory(path.parent, missing_ok=True).mkdir(exist_ok=True)
            raw = self._identity() | asdict(state)
            write_durable_json(path, raw | {'state_sha256': _digest(raw)})
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise self._error(f'estado não pôde ser gravado: {exc}') from exc

    def snapshot(self) -> StudyStateSnapshot:
        state = self._load()
        if state is None:
            return StudyStateSnapshot(self.path)
        return StudyStateSnapshot(self.path, state.owner, state.revision, state.closed, state.records)

    @property
    def exists(self):
        return self.snapshot().exists

    @property
    def owner(self):
        return self.snapshot().owner

    @property
    def revision(self):
        return self.snapshot().revision

    @property
    def closed(self):
        return self.snapshot().closed

    @property
    def records(self):
        return self.snapshot().records

    @property
    def latest(self):
        return self.snapshot().latest

    @property
    def authorization(self):
        return self.snapshot().authorization

    def get(self, round_id: str):
        return self.snapshot().get(round_id)

    def _require_owner(self, state, owner):
        _check_owner(owner)
        if state is None:
            raise self._error('experimento sem dono registrado')
        if state.owner != owner:
            raise self._error(f'experimento pertence a {state.owner}, não a {owner}')

    def claim(self, owner: str):
        _check_owner(owner)
        state = self._load()
        if state is None:
            self._write(_State(owner))
        else:
            self._require_owner(state, owner)

    def _run_directory_exists(self, run_id):
        directory = self._lease.active_directory() / 'runs'
        try:
            require_directory(directory, missing_ok=True)
            try:
                mode = (directory / run_id).lstat().st_mode
            except FileNotFoundError:
                return False
            if not S_ISDIR(mode):
                raise ValueError('esperado diretório publicado sem symlink')
            return True
        except (OSError, ValueError) as exc:
            raise self._error(f'{run_id}: destino não verificável: {exc}') from exc

    def begin(self, owner: str, round_id: str, declaration_hash: str, run_id: str, *,
              predecessor_run_id: str | None = None, adjustment_hash: str | None = None,
              effective_hash: str | None = None, retry_of: str | None = None,
              intent_hash: str | None = None) -> tuple[RoundRecord, bool]:
        state = self._load()
        self._require_owner(state, owner)
        slug(round_id, 'study.round_id')
        _check_digest(declaration_hash, 'study.declaration_hash')
        for prior in state.records:
            if prior.round_id == round_id:
                if prior.declaration_hash != declaration_hash:
                    raise self._error(f'{round_id}: ID de rodada já vinculado a outra declaração')
                return prior, False
        if state.closed:
            raise self._error(f'experimento já encerrado por {owner}')
        latest = state.records[-1] if state.records else None
        if latest is not None and latest.status in PENDING:
            raise self._error(f'{latest.run_id}: rodada ainda em {latest.status}')
        record = RoundRecord(owner, round_id, declaration_hash, run_id, predecessor_run_id,
                             adjustment_hash, effective_hash, retry_of=retry_of, intent_hash=intent_hash)
        _check_round(record)
        predecessor = next((row for row in reversed(state.records) if row.status == 'reviewed'), None)
        if not _permits(predecessor, record, latest):
            raise self._error(f'{round_id}: autorização de avanço incompatível com o predecessor')
        if any(row.run_id == run_id for row in state.records) or self._run_directory_exists(run_id):
            raise self._error(f'{run_id}: ID de execução já utilizado')
        self._write(replace(state, revision=state.revision + 1, records=state.records + (record,)))
        self._new_attempts.add(run_id)
        return record, True

    def _lookup(self, round_id):
        slug(round_id, 'study.round_id')
        state = self._load()
        for index, record in enumerate(state.records if state is not None else ()):
            if record.round_id == round_id:
                return state, index, record
        raise self._error(f'{round_id}: não há checkpoint para a rodada')

    def _replace_record(self, state, index, record):
        self._write(replace(state, revision=state.revision + 1,
                            records=state.records[:index] + (record,) + state.records[index + 1:]))
        return record

    def reconcile(self, round_id: str, verify: Callable[[RoundRecord], VerifiedPublication | None]):
        """Verify the exact destination and never search for similar or newer runs."""
        state, index, record = self._lookup(round_id)
        run_id = record.run_id
        if record.status == 'prepublication_failed':
            raise self._error(f'{run_id}: tentativa falhou comprovadamente antes da publicação')
        if not self._run_directory_exists(run_id):
            raise self._error(f'{run_id}: publicação ausente; resultado da tentativa não verificável')
        try:
            publication = verify(record)
            if not isinstance(publication, VerifiedPublication):
                raise ValueError('pacote não verificado')
            _check_digest(publication.evidence_sha256, 'study.evidence_sha256')
            expected = VerifiedPublication(
                self._lease.proposal_id, self._experiment, 'development', record.owner,
                record.round_id, record.declaration_hash, run_id, record.predecessor_run_id,
                record.adjustment_hash, record.evidence_sha256 or publication.evidence_sha256,
                record.effective_hash, record.retry_of, record.intent_hash,
            )
            if publication != expected:
                raise ValueError('pacote publicado não corresponde ao checkpoint')
            # A process crash can leave a visible rename before the publisher
            # flushes runs/. Sync its directory entry before the state write
            # (which syncs the proposal directory if runs/ was newly created).
            if record.status == 'dispatching' and os.name != 'nt':
                fd = os.open(self._lease.active_directory() / 'runs', os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise self._error(f'{run_id}: publicação não verificável: {exc}') from exc
        if record.status == 'dispatching':
            return self._replace_record(state, index, replace(
                record, status='published', evidence_sha256=publication.evidence_sha256))
        return record

    def fail_before_publication(self, round_id: str):
        """Mark a failure *proven by the caller* to precede any publication.

        This is only for the same call stack that began the dispatch under the
        current lease. Never use it for generic publication errors or recovery.
        """
        state, index, record = self._lookup(round_id)
        if (record.status != 'dispatching' or record.run_id not in self._new_attempts
                or self._run_directory_exists(record.run_id)):
            raise self._error(f'{record.run_id}: falha anterior à publicação não comprovada')
        failed = self._replace_record(state, index, replace(record, status='prepublication_failed'))
        self._new_attempts.remove(record.run_id)
        return failed

    def record_review(self, owner: str, round_id: str, evidence_sha256: str,
                      review: dict[str, Any], *, advance: AdvanceAuthorization | None = None):
        """Store an independently verified canonical decision and exact permission together."""
        state, index, record = self._lookup(round_id)
        self._require_owner(state, owner)
        if record.status not in ('published', 'reviewed') or record.evidence_sha256 != evidence_sha256:
            raise self._error(f'{record.run_id}: revisão não corresponde à publicação verificada')
        try:
            if type(review) is not dict or not review:
                raise ValueError('esperado objeto JSON canônico da revisão')
            canonical = _canonical(review)
            if advance is not None:
                _check_advance(advance)
                if any(row.round_id == advance.round_id for row in state.records[:index + 1]):
                    raise ValueError('autorização reutiliza ID de rodada')
        except (ValueError, TypeError, RecursionError) as exc:
            raise self._error(f'{record.run_id}: revisão inválida: {exc}') from exc
        if record.status == 'reviewed':
            if _canonical(record.review) != canonical or record.advance != advance:
                raise self._error(f'{record.run_id}: a primeira revisão não pode ser sobrescrita')
            return record
        return self._replace_record(state, index, replace(
            record, status='reviewed', review=json.loads(canonical), advance=advance))

    def close(self, owner: str):
        state = self._load()
        self._require_owner(state, owner)
        if state.closed:
            return
        if state.records and state.records[-1].status in PENDING:
            raise self._error(f'{state.records[-1].run_id}: não pode encerrar com rodada pendente')
        self._write(replace(state, closed=True, revision=state.revision + 1))
