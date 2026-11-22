"""Verified development dispatch and evidence-bound reviews shared by both routes."""

import hashlib
import json
from copy import deepcopy

import yaml

from . import research_model
from .contracts import Invalid, keys, text
from .diagnose import report_from_evidence
from .evidence import EVIDENCE_NAME, PrePublicationFailure, new_run_id, publish_study_run
from .graph import require_supported_route
from .guard import proposal_guard
from .layout import project_run_directory
from .partitions import record_development_exposure
from .review import open_verified_run
from .study import CONCLUSIONS, load_study, parse_study_declaration
from .study_state import STANDALONE, AdvanceAuthorization, StudyState, VerifiedPublication
from .validation import (
    artifact_paths,
    parse_yaml,
    read_limited,
    require_version,
    validate_documents,
    validate_proposal,
)

REVIEW_VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def valid(result):
    if not result.valid():
        issue = result.issues[0]
        raise Invalid(issue.path, issue.message)
    return result


def receipt_at(lease, run_id):
    return project_run_directory(lease.workspace.root, lease.proposal_id, run_id)


def verify_publication(lease, record):
    with open_verified_run(receipt_at(lease, record.run_id), expected_proposal=lease.proposal_id,
                           expected_run=record.run_id) as (receipt, _):
        extension = receipt["study"]
        binding = extension["binding"]
        effective = binding["effective_hash"]
        if digest(receipt["effective_experiment"]) != effective:
            raise Invalid("run.effective_experiment", "hash do experimento efetivo não confere")
        return VerifiedPublication(
            receipt["proposal"], receipt["experiment"], receipt["phase"], binding["managed_owner"],
            extension["round_id"], extension["declaration_hash"], receipt["run_id"],
            binding["predecessor_run_id"], binding["adjustment_hash"], receipt["artifact_hashes"][EVIDENCE_NAME],
            effective, binding.get("retry_of"), binding.get("intent_hash"),
        )


def reconcile_round(lease, state, record):
    return state.reconcile(record.round_id, lambda stored: verify_publication(lease, stored))


def read_managed_receipt(lease, record, *, diagnosis=False):
    with open_verified_run(receipt_at(lease, record.run_id), expected_proposal=lease.proposal_id,
                           expected_run=record.run_id) as (receipt, db):
        if receipt["artifact_hashes"][EVIDENCE_NAME] != record.evidence_sha256:
            raise Invalid("review.evidence_sha256", "evidência diverge do checkpoint")
        return (receipt, report_from_evidence(receipt, db)) if diagnosis else receipt


def run_summary(lease, record, *, receipt=None):
    if receipt is None:
        receipt = read_managed_receipt(lease, record)
    result = {key: receipt[key] for key in ("proposal", "experiment", "run_id", "result_kind", "status", "verdict", "metrics")}
    return result | {"manifest": str(receipt_at(lease, record.run_id) / "run.yaml"),
                     "round_id": record.round_id, "evidence_sha256": record.evidence_sha256,
                     "declaration_hash": record.declaration_hash}


def adjusted_validation(lease, experiment_id, adjustments):
    from .iteration import _apply_adjustment

    proposal_path, protocol_path = (lease.active_directory() / artifact_paths()[name]
                                    for name in ("proposal", "protocol"))
    proposal_raw, protocol_raw = (read_limited(path) for path in (proposal_path, protocol_path))
    if adjustments:
        raw = parse_yaml(protocol_raw, "protocol.yaml")
        for item in adjustments:
            raw = _apply_adjustment(raw, experiment_id, item)
        protocol_raw = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode()
    return valid(validate_documents(proposal_raw, protocol_raw, expected_id=lease.proposal_id,
                                    workspace=lease.workspace))


def _study_predecessor(lease, state):
    predecessor = next((row for row in reversed(state.records) if row.status == "reviewed"), None)
    if predecessor is None:
        return None, None
    predecessor = reconcile_round(lease, state, predecessor)
    return predecessor, read_managed_receipt(lease, predecessor)


def dispatch_study(lease, state, owner, base, study, *, data_root=None, confirmation_window=None,
                   plan_hash=None, retry=False, export_research_model=False):
    """Start only exact authorized work; reconcile a published checkpoint on retries."""
    from .runner import execute_study, preflight_study

    if state.owner not in (None, owner):
        raise Invalid("study.owner", f"o experimento pertence a {state.owner}")
    existing = state.get(study.round_id)
    if existing is not None:
        if retry:
            raise Invalid("study.round_id", "uma nova tentativa exige novo ID de rodada")
        if existing.declaration_hash != study.digest:
            raise Invalid("study.round_id", "ID já vinculado a outra declaração")
        return reconcile_round(lease, state, existing)
    if state.closed:
        raise Invalid("study", "desenvolvimento encerrado")
    latest = state.latest
    if latest is not None and latest.status in {"dispatching", "published"}:
        latest = reconcile_round(lease, state, latest)
        raise Invalid("study.review", f"run pendente de revisão: {latest.run_id}")
    intent = study.as_dict()
    del intent["round_id"]
    intent_hash = digest(intent)
    if retry and (latest is None or latest.status != "prepublication_failed" or latest.intent_hash != intent_hash):
        raise Invalid("study.retry", "nova tentativa exige a mesma declaração salvo round_id e falha comprovada")
    predecessor, prior_receipt = _study_predecessor(lease, state)
    experiment = base.definition.experiments[study.experiment]
    require_supported_route(experiment, standalone_study=owner == STANDALONE and confirmation_window is None)
    base_hash = digest(experiment.as_dict())
    changes, adjustment = [], None
    if predecessor is not None:
        binding = prior_receipt["study"]["binding"]
        if base_hash != binding["base_hash"] or plan_hash != binding["plan_hash"]:
            raise Invalid("study", "protocolo ou plano difere do predecessor verificado")
        changes = deepcopy(binding["adjustments"])
        authority = state.authorization
        if not retry and (authority is None or
                          (authority.round_id, authority.declaration_hash) != (study.round_id, study.digest)):
            raise Invalid("study.declaration_hash", f"declaração não autorizada pelo predecessor {predecessor.run_id}")
        adjustment = predecessor.review.get("adjustment")
        if adjustment is not None:
            changes.append(adjustment)
    effective = adjusted_validation(lease, study.experiment, changes) if changes else base
    experiment = effective.definition.experiments[study.experiment]
    effective_hash = digest(experiment.as_dict())
    if predecessor is not None and adjustment is None and effective_hash != predecessor.effective_hash:
        raise Invalid("study", "investigação não pode trocar a configuração efetiva")
    root = lease.workspace.effective_data_root(data_root)
    manifest = preflight_study(experiment, study, root, confirmation_window=confirmation_window)
    record_development_exposure(lease, experiment, manifest)
    state.claim(owner)
    adjustment_digest = digest(adjustment) if adjustment is not None else None
    binding = {
        "managed_owner": owner, "base_hash": base_hash, "plan_hash": plan_hash,
        "adjustments": changes, "adjustment_hash": adjustment_digest,
        "predecessor_run_id": predecessor.run_id if predecessor else None,
        "effective_hash": effective_hash, "intent_hash": intent_hash,
        "retry_of": latest.round_id if retry else None,
    }
    if owner != STANDALONE:
        binding |= {"iteration_id": owner, "round": len(state.records)}
    record, created = state.begin(owner, study.round_id, study.digest, new_run_id(),
                                  predecessor_run_id=binding["predecessor_run_id"],
                                  adjustment_hash=adjustment_digest, effective_hash=effective_hash,
                                  retry_of=binding["retry_of"], intent_hash=intent_hash)
    if not created:
        return reconcile_round(lease, state, record)
    try:
        result = execute_study(lease, experiment, study, root, confirmation_window=confirmation_window,
                               authorized_manifest=manifest)
        if export_research_model:
            binding[research_model.FIELD] = research_model.stage(lease, experiment, result, record.run_id)
    except (Invalid, OSError):
        # Execution or weights staging failed before canonical publication.
        state.fail_before_publication(record.round_id)
        raise
    try:
        publish_study_run(lease, effective, experiment, result, run_id=record.run_id, metadata=binding)
    except PrePublicationFailure:
        state.fail_before_publication(record.round_id)
        raise
    return reconcile_round(lease, state, record)


def run_standalone(lease, validation, experiment, state, study_path, *, data_root=None,
                   export_research_model=False):
    if state.owner not in (None, STANDALONE):
        raise Invalid("study.owner", f"use a iteração proprietária {state.owner}")
    if study_path is None:
        latest = state.latest
        pending = f"; run {latest.run_id} ({latest.status})" if latest is not None else ""
        raise Invalid("study", f"experimento gerenciado exige --study{pending}")
    study = load_study(study_path, validation.definition, experiment_id=experiment.id)
    latest = state.latest
    retry = bool(latest and latest.status == "prepublication_failed" and latest.round_id != study.round_id)
    record = dispatch_study(lease, state, STANDALONE, validation, study, data_root=data_root, retry=retry,
                            export_research_model=export_research_model)
    if not export_research_model:
        return run_summary(lease, record)
    receipt = read_managed_receipt(lease, record)
    output = run_summary(lease, record, receipt=receipt)
    try:
        output[research_model.FIELD] = research_model.finalize(lease, receipt)
    except (ValueError, OSError) as exc:
        output[research_model.FIELD] = {"status": "error", "reason": str(exc)}
    return output


def _review_record_path(path):
    return parse_yaml(read_limited(path), "review")


def _references(value, diagnosis):
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise Invalid("review.references", "esperada lista não vazia de referências")
    blocks = {row["block"]["id"]: row["diagnostics"] for row in diagnosis["blocks"]}
    checks = set(diagnosis["declaration"]["checks"])
    seen, matched, result = set(), set(), []
    for index, reference in enumerate(value):
        field = f"review.references.{index}"
        reference = keys(reference, field, required=("block_id", "measure"))
        block = text(reference["block_id"], f"{field}.block_id")
        measure = text(reference["measure"], f"{field}.measure")
        parts = measure.split(".")
        if (block, measure) in seen or block not in blocks or parts[0] not in checks:
            raise Invalid(field, "bloco, medida aplicável ou referência única não encontrada")
        observation = blocks[block]
        for component in parts:
            if not isinstance(observation, dict) or component not in observation:
                raise Invalid(f"{field}.measure", "medida não encontrada no diagnóstico verificado")
            observation = observation[component]
        if isinstance(observation, (dict, list)):
            raise Invalid(f"{field}.measure", "cite uma medida escalar, não uma seção inteira")
        matched.add(parts[0])
        seen.add((block, measure))
        result.append({"block_id": block, "measure": measure})
    if matched != checks:
        raise Invalid("review.references", "cite cada verificação declarada, inclusive a falta de suporte")
    return result


def _candidate_adjustment(lease, receipt, raw, plan):
    from .iteration import _control_nodes, _lookup_path, _normalize_change_path, validate_adjustment

    raw = keys(raw, "review.adjustment", required=("path", "value"))
    changes = receipt["study"]["binding"]["adjustments"]
    exp = adjusted_validation(lease, receipt["experiment"], changes).definition.experiments[receipt["experiment"]]
    effective = exp.as_dict()
    if plan is not None:
        adjusted = validate_adjustment(plan, raw, protocol=effective)
    else:
        path = _normalize_change_path(raw["path"], "review.adjustment.path")
        if path.split(".")[1] in _control_nodes(exp) or path.split(".")[1] == exp.result[0]:
            raise Invalid("review.adjustment.path", "somente parâmetros exclusivos do candidato são mutáveis")
        container, key = _lookup_path(effective, path)
        if container[key] == raw["value"]:
            raise Invalid("review.adjustment.value", "o valor não altera o candidato vigente")
        adjusted = raw
    adjusted_validation(lease, receipt["experiment"], [*changes, adjusted])
    return adjusted


def submit_round_review(lease, state, owner, run_id, path, *, plan=None):
    if state.owner != owner:
        raise Invalid("study.owner", f"revisão pertence a {state.owner or 'nenhum dono registrado'}")
    records = state.records
    record = next((row for row in records if row.run_id == run_id), None)
    if record is None:
        raise Invalid("review.run_id", "run não pertence à trilha gerenciada")
    record = reconcile_round(lease, state, record)
    receipt, diagnosis = read_managed_receipt(lease, record, diagnosis=True)
    raw = deepcopy(path) if isinstance(path, dict) else _review_record_path(path)
    raw = keys(raw, "review", required=("schema_version", "run_id", "evidence_sha256", "declaration_hash",
                                        "references", "conclusion", "rationale", "decision"),
               optional=("gap", "next_study", "adjustment"))
    require_version(raw, "review", REVIEW_VERSION)
    for name in ("run_id", "evidence_sha256", "declaration_hash"):
        if raw[name] != diagnosis[name]:
            raise Invalid(f"review.{name}", "não corresponde à evidência verificada")
    conclusion = text(raw["conclusion"], "review.conclusion")
    decision = text(raw["decision"], "review.decision")
    if conclusion not in CONCLUSIONS:
        raise Invalid("review.conclusion", "conclusão não suportada")
    if (decision not in {"stop", "investigate", "test_adjustment"}
            or (conclusion == "inconclusive" and decision == "test_adjustment")):
        raise Invalid("review.decision", "decisão incompatível com a conclusão")
    text(raw["rationale"], "review.rationale")
    _references(raw["references"], diagnosis)
    if decision == "investigate":
        text(raw.get("gap"), "review.gap")
    elif "gap" in raw:
        raise Invalid("review.gap", "campo exclusivo de investigações")
    if ("adjustment" in raw) != (decision == "test_adjustment"):
        raise Invalid("review.adjustment", "ajuste é obrigatório e exclusivo de test_adjustment")
    if (decision == "stop" and "next_study" in raw) or (decision == "test_adjustment" and "next_study" not in raw):
        raise Invalid("review.next_study", "a decisão requer uma declaração de avanço compatível")
    advance = None
    if "adjustment" in raw:
        raw["adjustment"] = _candidate_adjustment(lease, receipt, raw["adjustment"], plan)
    if "next_study" in raw:
        # 'protocol' is deliberately not a moving alias after a plan is frozen.
        old = diagnosis["declaration"]["blocks"]
        windows = plan.development_windows if plan is not None else valid(validate_proposal(
            lease.workspace, lease.proposal_id)).definition.experiments[receipt["experiment"]].windows
        study = parse_study_declaration(raw["next_study"], receipt["proposal"], receipt["experiment"], windows)
        if [block.as_dict() for block in study.blocks] != old:
            raise Invalid("review.next_study.blocks", "a próxima rodada deve preservar os blocos em ordem")
        raw["next_study"] = study.as_dict()
        advance = AdvanceAuthorization(study.round_id, study.digest,
                                       digest(raw["adjustment"]) if "adjustment" in raw else None)
    result = state.record_review(owner, record.round_id, record.evidence_sha256, raw, advance=advance)
    if owner == STANDALONE and decision == "stop":
        state.close(owner)
    return result


def record_standalone_review(root, proposal, run_id, path):
    with proposal_guard(root, proposal) as lease:
        with open_verified_run(receipt_at(lease, run_id), expected_proposal=proposal, expected_run=run_id) as (receipt, _):
            experiment = receipt["experiment"]
        state = StudyState(lease, experiment)
        record = submit_round_review(lease, state, STANDALONE, run_id, path)
        return {"schema_version": REVIEW_VERSION, "run_id": run_id, "status": "recorded", "review": record.review}
