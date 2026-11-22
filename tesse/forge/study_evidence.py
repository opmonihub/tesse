"""The study-v1 tables inside the ordinary portable Forge evidence database."""

import hashlib
import json
from math import isfinite

from .contracts import Invalid, keys
from .study import parse_study_declaration

EXTENSION = "study-v1"
PHASES = ("calibration", "evaluation")
ARMS = ("control", "candidate")
SELECTION = ("below_cutoff", "proposed_raw", "proposed_core", "proposed_margin", "below_margin", "outside_pool")
OPPORTUNITY_FIELDS = (
    "asset", "anchor_at", "decision_at", "entry_at", "expiry_at", "label_at", "direction",
    "raw_score", "probability_win", "selection", "replay", "blocking_anchor_at", "outcome",
)
SCHEMA = {
    "study_plan": """CREATE TABLE study_plan (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_name TEXT NOT NULL CHECK (schema_name = 'study-v1'),
    declaration_hash TEXT NOT NULL CHECK (length(declaration_hash) = 64),
    declaration_json TEXT NOT NULL,
    binding_json TEXT NOT NULL
) STRICT""",
    "study_blocks": """CREATE TABLE study_blocks (
    block_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL UNIQUE CHECK (position >= 0),
    metrics_json TEXT NOT NULL,
    gates_json TEXT NOT NULL,
    training_json TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('passed', 'failed'))
) STRICT""",
    "study_cutoffs": """CREATE TABLE study_cutoffs (
    block_id TEXT NOT NULL REFERENCES study_blocks (block_id),
    phase TEXT NOT NULL CHECK (phase IN ('calibration', 'evaluation')),
    arm TEXT NOT NULL CHECK (arm IN ('control', 'candidate')),
    cutoffs_json TEXT NOT NULL,
    PRIMARY KEY (block_id, phase, arm)
) STRICT""",
    "study_opportunities": f"""CREATE TABLE study_opportunities (
    block_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    arm TEXT NOT NULL,
    asset TEXT NOT NULL,
    anchor_at TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    entry_at TEXT NOT NULL,
    expiry_at TEXT NOT NULL,
    label_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('CALL', 'PUT')),
    raw_score REAL NOT NULL,
    probability_win REAL CHECK (probability_win BETWEEN 0.0 AND 1.0),
    selection TEXT NOT NULL CHECK (selection IN {SELECTION}),
    replay TEXT CHECK (replay IN ('outside_window', 'blocked_overlap', 'admitted', 'settled')),
    blocking_anchor_at TEXT,
    outcome TEXT CHECK (outcome IN ('WIN', 'LOSS', 'TIE')),
    CHECK ((arm = 'control') = (probability_win IS NULL)),
    CHECK ((selection LIKE 'proposed_%') = (replay IS NOT NULL)),
    CHECK ((replay IS 'blocked_overlap') = (blocking_anchor_at IS NOT NULL)),
    CHECK (replay IS NOT 'settled' OR (phase = 'evaluation' AND outcome IS NOT NULL)),
    CHECK (replay IS NOT 'admitted' OR phase = 'calibration'),
    FOREIGN KEY (block_id, phase, arm) REFERENCES study_cutoffs (block_id, phase, arm),
    PRIMARY KEY (block_id, phase, arm, asset, anchor_at)
) STRICT""",
}
CUTS = {
    "control": {"raw_cutoff"},
    "candidate": {"pool_cutoff", "core_cutoff", "margin_score_cutoff", "margin_base_cutoff"},
}


def _dumps(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)



def canonical_digest(value):
    return hashlib.sha256(_dumps(value).encode()).hexdigest()

def _invalid(detail):
    return Invalid("evidence.sqlite.study", detail)


def _loads(value):
    try:
        result = json.loads(value)
        if _dumps(result) == value:
            return result
    except (ValueError, TypeError, OverflowError):
        pass
    raise _invalid("JSON diagnóstico inválido ou não canônico")


def _valid_cuts(cuts, arm):
    return (isinstance(cuts, dict) and set(cuts) == CUTS[arm]
            and all(type(v) in (int, float) and isfinite(v) for v in cuts.values()))


def write_extension(db, study, records, executions, binding):
    """Append the extension while the canonical trade transaction is still open."""
    for sql in SCHEMA.values():
        db.execute(sql)
    db.execute("INSERT INTO study_plan VALUES (1, ?, ?, ?, ?)",
               (EXTENSION, study.digest, _dumps(study.as_dict()), _dumps(binding)))
    markers = ",".join("?" for _ in range(len(OPPORTUNITY_FIELDS) + 3))
    count = 0
    for position, block in enumerate(study.blocks):
        result = executions[block.id]
        metrics, gates, verdict = records[block.id]
        db.execute("INSERT INTO study_blocks VALUES (?, ?, ?, ?, ?, ?)",
                   (block.id, position, _dumps(metrics), _dumps(gates), _dumps(result.training), verdict))
        for arm in ARMS:
            traces = getattr(result.output, arm).trace
            if traces is None or set(traces) != set(PHASES):
                raise _invalid(f"trilha incompleta: {block.id}/{arm}")
            for phase in PHASES:
                trace = traces[phase]
                if not _valid_cuts(trace.cutoffs, arm):
                    raise _invalid(f"cortes inválidos: {block.id}/{phase}/{arm}")
                db.execute("INSERT INTO study_cutoffs VALUES (?, ?, ?, ?)",
                           (block.id, phase, arm, _dumps(trace.cutoffs)))
                db.executemany(f"INSERT INTO study_opportunities VALUES ({markers})", (
                    (block.id, phase, arm, *(row[field] for field in OPPORTUNITY_FIELDS))
                    for row in trace.rows
                ))
                count += len(trace.rows)
    return count


def verify_extension(db, receipt, actual_tables):
    declared = keys(receipt["study"], "run.yaml.study", required=(
        "schema", "round_id", "declaration_hash", "declaration", "opportunity_count", "binding",
    ))
    if declared["schema"] != EXTENSION:
        raise _invalid("versão de diagnóstico desconhecida")
    if receipt.get("phase") != "development" or receipt.get("result_kind") != "comparison":
        raise _invalid("extensão exige comparação de desenvolvimento")
    if set(actual_tables) != {"trades", *SCHEMA}:
        raise _invalid("tabelas incompatíveis com a extensão declarada")
    actual_sql = dict(db.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'table'"))
    if any(actual_sql[name] != sql for name, sql in SCHEMA.items()):
        raise _invalid("schema de tabela diagnóstica incompatível")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise _invalid("referência diagnóstica inválida")
    rows = db.execute("SELECT schema_name, declaration_hash, declaration_json, binding_json FROM study_plan").fetchall()
    if len(rows) != 1:
        raise _invalid("esperada uma única declaração")
    version, digest, raw, binding = rows[0]
    document = _loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("blocks"), list):
        raise _invalid("plano efetivo não declara blocos")
    study = parse_study_declaration(document, receipt["proposal"], receipt["experiment"], {})
    if (version != EXTENSION or digest != declared["declaration_hash"] or study.digest != digest
            or study.round_id != declared["round_id"] or document != declared["declaration"]
            or _loads(binding) != declared["binding"]
            or not isinstance(declared["binding"], dict)
            or any(receipt.get(key) != value for key, value in declared["binding"].items())):
        raise _invalid("declaração ou vínculo no recibo diverge do pacote selado")
    if (declared["binding"].get("effective_hash") != canonical_digest(receipt.get("effective_experiment"))
            or declared["binding"].get("snapshot_identity") != receipt.get("snapshot")):
        raise _invalid("proveniência do snapshot ou experimento efetivo diverge")
    _verify_blocks(db, receipt, study)
    observed = db.execute("SELECT COUNT(*) FROM study_opportunities").fetchone()[0]
    if type(declared["opportunity_count"]) is not int or observed != declared["opportunity_count"]:
        raise _invalid("contagem de oportunidades diverge do recibo")
    # A settled evaluation row must correspond one-to-one to an actual trade,
    # including its direction, clock and official outcome. All other outcomes
    # are hypothetical and cannot acquire a link to trades.
    trade_count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    settled_count = db.execute("SELECT COUNT(*) FROM study_opportunities WHERE replay = 'settled'").fetchone()[0]
    matches = db.execute("""
        SELECT COUNT(DISTINCT t.rowid) FROM trades t
        JOIN study_opportunities o USING (arm, asset, anchor_at, direction, outcome,
                                            decision_at, entry_at, expiry_at, label_at)
        WHERE o.replay = 'settled'
    """).fetchone()[0]
    if trade_count != settled_count or trade_count != matches:
        raise _invalid("liquidações diagnósticas e trades oficiais divergem")


def _verify_blocks(db, receipt, study):
    ids = [block.id for block in study.blocks]
    rows = db.execute("SELECT block_id, position, metrics_json, gates_json, training_json, verdict FROM study_blocks ORDER BY position").fetchall()
    if [(row[0], row[1]) for row in rows] != list(zip(ids, range(len(ids)), strict=True)):
        raise _invalid("blocos efetivos em ordem incompatível")
    summary = {}
    for block, _, metrics, gates, training, verdict in rows:
        metrics, gates, training = map(_loads, (metrics, gates, training))
        names = {"min_winrate", "min_trades_per_day", "min_improvement_pp"}
        if (not isinstance(gates, dict) or set(gates) != names or any(type(v) is not bool for v in gates.values())
                or verdict != ("passed" if all(gates.values()) else "failed")):
            raise _invalid(f"gates ou veredito inválidos: {block}")
        summary[block] = {"metrics": metrics, "criteria": gates, "training": training, "verdict": verdict}
    if (receipt["metrics"] != {"blocks": {key: row["metrics"] for key, row in summary.items()}}
            or receipt.get("training") != {"blocks": {key: row["training"] for key, row in summary.items()}}
            or receipt.get("checks", {}).get("criteria") != {key: row["criteria"] for key, row in summary.items()}
            or receipt["verdict"] != ("passed" if all(row["verdict"] == "passed" for row in summary.values()) else "failed")):
        raise _invalid("resultados por bloco divergem do recibo")
    cuts = {(block, phase, arm): _loads(value) for block, phase, arm, value in db.execute(
        "SELECT block_id, phase, arm, cutoffs_json FROM study_cutoffs"
    )}
    if (set(cuts) != {(block, phase, arm) for block in ids for phase in PHASES for arm in ARMS}
            or any(not _valid_cuts(value, arm) or value != cuts[block, "evaluation", arm]
                   for (block, _, arm), value in cuts.items())):
        raise _invalid("cortes congelados incompletos ou incompatíveis entre fases")


def read_block(db, block_id):
    metrics, gates, training, verdict = db.execute(
        "SELECT metrics_json,gates_json,training_json,verdict FROM study_blocks WHERE block_id = ?", (block_id,)
    ).fetchone()
    names = ("phase", "arm", *OPPORTUNITY_FIELDS)
    rows = (dict(zip(names, row, strict=True)) for row in db.execute(
        f"SELECT {','.join(names)} FROM study_opportunities WHERE block_id = ? ORDER BY phase, arm, asset, anchor_at", (block_id,)
    ))
    cuts = {phase: {} for phase in PHASES}
    for phase, arm, value in db.execute(
        "SELECT phase,arm,cutoffs_json FROM study_cutoffs WHERE block_id = ? ORDER BY phase,arm", (block_id,)
    ):
        cuts[phase][arm] = _loads(value)
    return {"metrics": _loads(metrics), "criteria": _loads(gates), "training": _loads(training),
            "verdict": verdict, "cutoffs": cuts}, rows
