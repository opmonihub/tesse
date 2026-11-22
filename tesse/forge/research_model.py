"""Study-bound, native research weights; never part of the canonical Forge run."""

import hashlib
import json
from pathlib import Path
from re import fullmatch
from tempfile import TemporaryDirectory

from ..adapters.xgboost_adapter import XGB_KEY
from .catalog import VOLATILITY_SCORE
from .components import VOLATILITY_LOOKBACK
from .contracts import Invalid, slug
from .evidence import EVIDENCE_NAME, _sync_directory, _sync_file, sha256_file
from .layout import require_directory, validate_run_id
from .study_state import STANDALONE

SCHEMA = 'forge-xgboost-models-v1'
FIELD = 'research_model'
MANIFEST = 'manifest.json'


def _graph_model(experiment):
    """Follow the candidate's calibration/model ports, not a conventional node ID."""
    nodes = experiment['nodes']

    def resolve(node, port, expected):
        node_id = node['inputs'][port].split('.')[0]
        result = nodes[node_id]
        if result['component'] != expected + '@1.0.0':
            raise Invalid(FIELD, f'esperado {expected} na porta {port}')
        return node_id, result

    comparison = nodes[experiment['result']['from'].split('.')[0]]
    _, candidate = resolve(comparison, 'candidate', 'replay.option_m1')
    _, selection = resolve(candidate, 'proposals', 'selection.edge_rank')
    _, predictor = resolve(selection, 'calibration', 'predict.xgboost')
    learner_id, learner = resolve(predictor, 'model', 'learner.xgboost')
    _, split = resolve(learner, 'train', 'split.temporal')
    score = nodes[split['inputs']['scores'].split('.')[0]]
    if score['component'] not in ('score.directional_return@1.0.0', VOLATILITY_SCORE):
        raise Invalid(FIELD, 'score sem contrato de features para exportação')
    _, features = resolve(score, 'features', 'features.returns')
    period = score['params']['period']
    order = [
        {'name': 'raw', 'lookback_bars': period, 'transform': 'absolute_close_return'},
        *({'name': f'r{p}', 'lookback_bars': p, 'transform': 'close_return_times_side'}
          for p in features['params']['periods']),
    ]
    if score['component'] == VOLATILITY_SCORE:
        order.append({'name': 'volatility15', 'lookback_bars': VOLATILITY_LOOKBACK,
                      'transform': 'population_std_of_close_return', 'return_lookback_bars': 1,
                      'ddof': 0, 'oriented': False})
    return learner_id, order


def _root(lease, *, create=False):
    root = require_directory(lease.active_directory() / 'research-models', missing_ok=create)
    if create:
        root.mkdir(exist_ok=True)
        _sync_directory(root.parent)
    return root


def _move_new(source, destination):
    # Both paths are under the same proposal-wide writer lease. Never replace a
    # preexisting file, directory (including empty), or dangling link.
    if destination.exists() or destination.is_symlink():
        raise Invalid(FIELD, 'destino da publicação já existe')
    source.rename(destination)


def _write_new(path, content):
    with path.open('xb') as stream:
        stream.write(content)
    _sync_file(path)


def stage(lease, experiment, execution, run_id):
    """Persist this execution's model bytes, and return only their sealed identities."""
    validate_run_id(run_id)
    learner, _ = _graph_model(experiment.as_dict())
    root = _root(lease, create=True)
    staging = require_directory(root / '.staging', missing_ok=True)
    staging.mkdir(exist_ok=True)
    _sync_directory(root)
    with TemporaryDirectory(prefix='.draft-', dir=staging) as scratch:
        draft = Path(scratch) / run_id
        models = draft / 'models'
        models.mkdir(parents=True)
        entries = []
        for block_id, execution_block in execution.blocks.items():
            path = models / (slug(block_id, FIELD) + '.json')
            fitted = execution_block.state['models'].get(learner)
            if fitted is None or fitted.receipt.get('adapter') != XGB_KEY:
                raise Invalid(FIELD, f'modelo ajustado não encontrado: {block_id}')
            try:
                content = fitted.adapter.model_json(fitted.model)
            except (ValueError, RuntimeError) as exc:
                raise Invalid(FIELD, f'JSON nativo indisponível: {block_id}: {exc}') from exc
            _write_new(path, content)
            entries.append({'block_id': block_id, 'sha256': hashlib.sha256(content).hexdigest()})
        _sync_directory(models)
        _sync_directory(draft)
        _move_new(draft, staging / run_id)
        _sync_directory(staging)
    return {'schema': SCHEMA, 'models': entries}


def _manifest(receipt):
    study = receipt['study']
    binding = study['binding']
    declared = binding.get(FIELD)
    if declared is None:
        raise Invalid(FIELD, 'rodada já publicada sem solicitação de exportação; exportação retroativa proibida')
    if (receipt['phase'] != 'development' or binding['managed_owner'] != STANDALONE
            or not isinstance(declared, dict) or declared.get('schema') != SCHEMA):
        raise Invalid(FIELD, 'vínculo de exportação incompatível')
    learner, feature_order = _graph_model(receipt['effective_experiment'])
    entries = declared.get('models')
    blocks = study['declaration']['blocks']
    if not isinstance(entries, list) or len(entries) != len(blocks):
        raise Invalid(FIELD, 'vínculo não corresponde aos blocos declarados')
    records = []
    for block, entry in zip(blocks, entries, strict=True):
        block_id = slug(block['id'], FIELD)
        if (not isinstance(entry, dict) or entry.get('block_id') != block_id
                or not isinstance(entry.get('sha256'), str) or not fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise Invalid(FIELD, 'hash ou bloco incompatível com o plano selado')
        fit = receipt['training']['blocks'][block_id][learner]
        provenance = fit['adapter_invocation']['provenance']
        if (fit['adapter'] != XGB_KEY or provenance['library_id'] != 'xgboost'
                or fit['resolved_version'] != fit['library_lock']['resolved_version']
                or fit['resolved_version'] != provenance['resolved_version']):
            raise Invalid(FIELD, f'proveniência do ajuste incompatível: {block_id}')
        records.append({
            'block_id': block_id, 'path': f'models/{block_id}.json', 'sha256': entry['sha256'],
            'windows': {key: block[key] for key in ('train', 'calibration', 'evaluation')},
            'fit': fit,
        })
    return {
        'schema': SCHEMA, 'scope': 'research_only',
        **{key: receipt[key] for key in ('proposal', 'experiment', 'run_id', 'verdict', 'snapshot')},
        **{key: study[key] for key in ('round_id', 'declaration_hash')},
        'effective_experiment_sha256': binding['effective_hash'],
        'evidence_sha256': receipt['artifact_hashes'][EVIDENCE_NAME],
        'feature_order': feature_order, 'learner_node': learner, 'learner_component': 'learner.xgboost@1.0.0',
        'features_dtype': 'float32', 'classes': {'0': 'non-WIN (including TIE)', '1': 'WIN'},
        'prediction': 'P(WIN of the declared side); does not choose the side',
        'side': 'sign of the directional close return: +1 CALL, -1 PUT; zero excluded',
        'label': {'entry_offset_seconds': 120, 'available_offset_seconds': 182,
                  'win': 'sign(close - open) of the entry candle equals the declared side'},
        'models': records,
    }


def _verify_files(directory, manifest, *, staged=False):
    require_directory(directory)
    weights = require_directory(directory / 'models')
    expected = {directory / item['path']: item['sha256'] for item in manifest['models']}
    if set(weights.iterdir()) != set(expected):
        raise Invalid(FIELD, 'pesos ausentes ou arquivos inesperados')
    for path, required_hash in expected.items():
        if path.is_symlink() or not path.is_file() or sha256_file(path) != required_hash:
            raise Invalid(FIELD, f'arquivo inseguro ou SHA-256 divergente: {path.name}')
    head = directory / MANIFEST
    if set(directory.iterdir()) != {weights} and set(directory.iterdir()) != {weights, head}:
        raise Invalid(FIELD, 'diretório de exportação contém artefatos inesperados')
    content = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + '\n').encode()
    if head.is_symlink():
        raise Invalid(FIELD, 'manifesto não pode ser um symlink')
    if head.exists() or not staged:
        if not head.is_file() or head.stat().st_size != len(content) or head.read_bytes() != content:
            raise Invalid(FIELD, 'manifesto do export ausente ou divergente do run verificado')
    return content


def finalize(lease, receipt):
    """Use only the verified receipt and retained bytes; no snapshot or library import."""
    manifest = _manifest(receipt)
    run_id = manifest['run_id']
    validate_run_id(run_id)
    root = _root(lease)
    destination = root / run_id
    if destination.exists() or destination.is_symlink():
        _verify_files(destination, manifest)
    else:
        staging = require_directory(root / '.staging')
        draft = staging / run_id
        content = _verify_files(draft, manifest, staged=True)
        if not (draft / MANIFEST).exists():
            # A crash during write must not leave an invalid manifest in the
            # recoverable stage. Assemble it separately, then install it whole.
            with TemporaryDirectory(prefix='.manifest-', dir=staging) as scratch:
                source = Path(scratch) / MANIFEST
                _write_new(source, content)
                _move_new(source, draft / MANIFEST)
        _sync_directory(draft)
        _move_new(draft, destination)
        _sync_directory(staging)
    _sync_directory(root)
    return {'status': 'published', 'path': str(destination), 'manifest': str(destination / MANIFEST),
            'schema': SCHEMA, 'evidence_sha256': manifest['evidence_sha256'],
            'models': [{key: item[key] for key in ('block_id', 'path', 'sha256')} for item in manifest['models']]}
