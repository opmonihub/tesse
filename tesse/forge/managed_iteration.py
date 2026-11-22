"""Managed Forge transitions; typed guidance mirrors the durable state, not its prose."""

from .contracts import Invalid, keys
from .diagnose import diagnose_project_run
from .managed import (
    _review_record_path,
    digest,
    dispatch_study,
    read_managed_receipt,
    reconcile_round,
    submit_round_review,
    valid,
)
from .study import load_study, parse_study_declaration
from .study_state import StudyState
from .validation import load_workspace, validate_proposal

PROTOCOL_VERSION = 1


def _action(action_id, authorization, reason, **args):
    return {"id": action_id, "authorization": authorization, "reason": reason, "arguments": args}


def _critic_state(checkpoint, record):
    return checkpoint.get("managed_critics", {}).get(record.run_id, {}) if record else {}


def _pending_critics(calls):
    return [role for role, result in calls.items() if result["status"] == "started"]


def _reviewer_calls(checkpoint):
    return sum(map(len, checkpoint.get("managed_critics", {}).values()))


def envelope(plan, iteration_id, view, checkpoint):
    record = view.latest
    remaining = {"rounds": max(0, plan.max_rounds - len(view.records)),
                 "reviewer_calls": max(0, plan.max_reviewer_calls - _reviewer_calls(checkpoint))}
    evidence = {"run_id": record.run_id, "declaration_hash": record.declaration_hash,
                "evidence_sha256": record.evidence_sha256} if record else None
    stop = checkpoint.get("managed_stop")
    blocker = checkpoint.get("managed_blocker")
    action, status = None, "ready"
    if view.owner != iteration_id:
        blocker = "owner_not_verified"
    if stop and view.closed:
        status = checkpoint.get("status", "stopped")
    elif blocker:
        status = "blocked"
        action = _action("resolve_blocker", "blocked", blocker)
    elif record and record.status == "dispatching":
        status = "reconciling"
        action = _action("resume", "ready", "publication_checkpoint", run_id=record.run_id)
    elif record and record.status == "prepublication_failed":
        status, blocker = "blocked", "prepublication_failed"
        if remaining["rounds"]:
            action = _action("retry_round", "ready", blocker, run_id=record.run_id)
        else:
            action = _action("resume", "ready", "round_budget_exhausted")
    elif record and record.status == "published":
        status = "awaiting_review"
        action = _action("review_round", "review_required", "review_missing", **evidence)
    elif record and _pending_critics(_critic_state(checkpoint, record)):
        status, blocker = "blocked", "critic_outcome_unknown"
        action = _action("resolve_blocker", "blocked", blocker, roles=_pending_critics(_critic_state(checkpoint, record)))
    elif (checkpoint.get("managed_pending_approval")
          and record.run_id not in checkpoint.get("managed_approvals", {})):
        pending = checkpoint["managed_pending_approval"]
        status = "awaiting_approval"
        action = _action("approve_adjustment", "approval_required", "exact_adjustment_required", **pending)
    else:
        action = _action("resume", "ready", "authorized_transition" if record else "initial_round")
    revision = digest({"plan": plan.plan_hash, "owner": view.revision, "checkpoint": checkpoint})
    if action is not None:
        action["arguments"].update(iteration_id=iteration_id, state_revision=revision)
    return {
        "schema_version": PROTOCOL_VERSION, "proposal_id": plan.proposal_id,
        "experiment_id": plan.experiment_id, "iteration_id": iteration_id, "plan_hash": plan.plan_hash,
        "round_id": record.round_id if record else None, "status": status, "evidence": evidence,
        "state_revision": revision, "next_action": action, "blocker_reason": blocker,
        "stop_reason": stop if view.closed else None,
        "budgets": {"rounds": {"remaining": remaining["rounds"], "limit": plan.max_rounds},
                    "reviewer_calls": {"remaining": remaining["reviewer_calls"], "limit": plan.max_reviewer_calls}},
        "freeze_eligible": bool(view.closed and stop == "target_met" and not checkpoint.get("candidate")),
        "confirmation": {key: checkpoint[key] for key in ("candidate", "confirmation_approval",
                                                              "confirmation_consumed", "confirmation_run_id")
                         if checkpoint.get(key) is not None},
    }


def inspect_managed(root, directory, plan, journal):
    workspace = load_workspace(root)
    view = StudyState.read_only(workspace, plan.proposal_id, plan.experiment_id)
    return envelope(plan, directory.name, view, journal.checkpoints())


def current(state, directory, plan, journal):
    return envelope(plan, directory.name, state.snapshot(), journal.checkpoints())


def check_revision(state, directory, plan, journal, expected=None):
    payload = current(state, directory, plan, journal)
    if expected is not None and expected != payload["state_revision"]:
        raise Invalid("iteration.state_revision", "ação obsoleta: consulte o estado corrente")
    if state.owner != directory.name:
        raise Invalid("study.owner", "posse da iteração não verificável")
    return payload


def _finish(state, owner, journal, reason):
    journal.checkpoint("managed_stop", reason)
    journal.checkpoint("status", "stopped")
    state.close(owner)


def _terminal_reason(record, receipt, plan, state):
    if receipt["verdict"] == "passed":
        return "target_met"
    if record.review["decision"] == "stop":
        return "stopped_by_review"
    if len(state.records) >= plan.max_rounds:
        return "budget_exhausted"
    if record.advance is None:
        return "no_justified_next_step"
    return None


def review_iteration_round(lease, directory, plan, journal, path, *, expected_revision=None):
    state = StudyState(lease, plan.experiment_id)
    check_revision(state, directory, plan, journal, expected_revision)
    raw = _review_record_path(path)
    run_id = raw.get("run_id")
    record = submit_round_review(lease, state, directory.name, run_id, raw, plan=plan)
    if record == state.latest and not state.closed:
        receipt = read_managed_receipt(lease, record)
        reason = _terminal_reason(record, receipt, plan, state)
        if reason:
            _finish(state, directory.name, journal, reason)
    return current(state, directory, plan, journal)


def diagnose_iteration_round(root, proposal, payload):
    evidence = payload["evidence"]
    if evidence is None or evidence["evidence_sha256"] is None:
        raise Invalid("study.diagnose", "não há run de desenvolvimento verificado")
    report = diagnose_project_run(root, proposal, evidence["run_id"])
    if report["evidence_sha256"] != evidence["evidence_sha256"] or report["status"] != "available":
        raise Invalid("study.diagnose", "diagnóstico não corresponde ao checkpoint")
    return payload | {"diagnosis": report}


def _required_critics(plan, wrapper, record, journal, reviewers):
    from .iteration import collect_critiques

    checkpoints = journal.checkpoints()
    all_calls = checkpoints.get("managed_critics", {})
    calls = all_calls.setdefault(record.run_id, {})
    if _pending_critics(calls):
        return "unknown", ()
    undispatched = [role for role in plan.reviewer_roles if role not in calls]
    if _reviewer_calls(checkpoints) + len(undispatched) > plan.max_reviewer_calls:
        return "budget", ()
    for role in undispatched:
        calls[role] = {"status": "started"}
        journal.checkpoint("managed_critics", all_calls)
        opinion, = collect_critiques(plan, wrapper, reviewers=reviewers, only_role=role)
        calls[role] = {"status": "completed", "response": opinion}
        journal.checkpoint("managed_critics", all_calls)
    return "completed", tuple(calls[role]["response"] for role in plan.reviewer_roles)


def advance(lease, directory, plan, journal, *, data_root=None, reviewers=None, expected_revision=None):
    from .iteration import reconcile_critiques

    state = StudyState(lease, plan.experiment_id)
    owner = directory.name
    payload = check_revision(state, directory, plan, journal, expected_revision)
    if data_root is None:
        data_root = journal.checkpoint("managed_data_root")
    if state.closed:
        return payload
    record = state.latest
    if payload["status"] == "blocked":
        if (payload["blocker_reason"] == "prepublication_failed"
                and payload["budgets"]["rounds"]["remaining"] == 0):
            _finish(state, owner, journal, "budget_exhausted")
            return current(state, directory, plan, journal)
        return payload
    if record and record.status in {"published", "dispatching"}:
        try:
            reconcile_round(lease, state, record)
        except Invalid:
            journal.checkpoint("managed_blocker", "publication_not_verified")
            raise
        return current(state, directory, plan, journal)
    base = valid(validate_proposal(lease.workspace, lease.proposal_id))
    if record is None:
        if plan.max_rounds == 0:
            _finish(state, owner, journal, "budget_exhausted")
        else:
            dispatch_study(lease, state, owner, base, plan.initial_study, data_root=data_root,
                           confirmation_window=plan.confirmation_window, plan_hash=plan.plan_hash)
        return current(state, directory, plan, journal)
    record = reconcile_round(lease, state, record)
    receipt, diagnosis = read_managed_receipt(lease, record, diagnosis=True)
    reason = _terminal_reason(record, receipt, plan, state)
    if reason:
        _finish(state, owner, journal, reason)
        return current(state, directory, plan, journal)
    pending = journal.checkpoint("managed_pending_approval")
    checkpoint = journal.checkpoints()
    approved = checkpoint.get("managed_approvals", {}).get(record.run_id)
    if pending and not approved:
        return payload
    adjustment = record.review.get("adjustment")
    evidence = {"run_id": record.run_id, "metrics": receipt["metrics"], "criteria": receipt["checks"]["criteria"],
                "protocol": receipt["effective_experiment"], "verdict": receipt["verdict"],
                "study": receipt["study"]["declaration"], "review": record.review,
                "evidence_sha256": record.evidence_sha256, "declaration_hash": record.declaration_hash,
                "snapshot": diagnosis["snapshot"], "diagnostics": diagnosis["blocks"]}
    critic_status, opinions = _required_critics(plan, evidence, record, journal, reviewers)
    if critic_status == "budget":
        _finish(state, owner, journal, "budget_exhausted")
        return current(state, directory, plan, journal)
    if critic_status == "unknown":
        return current(state, directory, plan, journal)
    outcome = reconcile_critiques(opinions)
    if outcome["status"] == "blocked":
        journal.checkpoint("managed_blocker", outcome["reason"])
        return current(state, directory, plan, journal)
    if adjustment is not None:
        if (outcome["status"] != "candidate"
                or _adjustment_value(outcome["adjustment"]) != adjustment):
            _finish(state, owner, journal, "no_justified_next_step")
            return current(state, directory, plan, journal)
        expected = {"predecessor_run_id": record.run_id, "declaration_hash": record.advance.declaration_hash,
                    "adjustment": adjustment, "adjustment_hash": record.advance.adjustment_hash}
        if approved is not None and approved != expected:
            raise Invalid("approval", "aprovação vinculada a outra rodada ou ajuste")
        if approved is None:
            if not plan.automatic:
                journal.checkpoint("managed_pending_approval", expected)
                return current(state, directory, plan, journal)
            _save_approval(journal, record.run_id, expected)
    study = parse_study_declaration(record.review["next_study"], plan.proposal_id, plan.experiment_id,
                                   plan.development_windows)
    if study.blocks != plan.initial_study.blocks:
        raise Invalid("study.blocks", "bateria não corresponde ao plano congelado")
    if pending:
        journal.checkpoint("managed_pending_approval", None)
    dispatch_study(lease, state, owner, base, study, data_root=data_root,
                   confirmation_window=plan.confirmation_window, plan_hash=plan.plan_hash)
    return current(state, directory, plan, journal)


def _adjustment_value(value):
    return {key: value[key] for key in ("path", "value")}


def _save_approval(journal, run_id, approval):
    approvals = journal.checkpoint("managed_approvals") or {}
    approvals[run_id] = approval
    journal.checkpoint("managed_approvals", approvals)


def approve(lease, directory, plan, journal, adjustment, *, data_root=None, expected_revision=None):
    state = StudyState(lease, plan.experiment_id)
    check_revision(state, directory, plan, journal, expected_revision)
    if expected_revision is None:
        raise Invalid("approval", "aprovação gerenciada exige --state-revision da ação pendente")
    pending = journal.checkpoint("managed_pending_approval")
    adjustment = keys(adjustment, "approval", required=("path", "value"))
    if (state.closed or not pending or pending["predecessor_run_id"] != state.latest.run_id
            or digest(adjustment) != pending["adjustment_hash"]):
        raise Invalid("approval", "aprovação não corresponde ao ajuste exato pendente")
    _save_approval(journal, state.latest.run_id, pending)
    return advance(lease, directory, plan, journal, data_root=data_root)


def freeze(lease, directory, plan, journal, run_id, *, expected_revision=None):
    state = StudyState(lease, plan.experiment_id)
    payload = check_revision(state, directory, plan, journal, expected_revision)
    record = state.latest
    if not payload["freeze_eligible"] or (run_id is not None and run_id != record.run_id):
        raise Invalid("candidate", "congelamento exige o run exato de uma bateria aprovada e revisada")
    record = reconcile_round(lease, state, record)
    receipt = read_managed_receipt(lease, record)
    if record.status != "reviewed" or receipt["verdict"] != "passed":
        raise Invalid("candidate", "run não é elegível para congelamento")
    journal.checkpoint("candidate", {"run_id": record.run_id, "evidence_sha256": record.evidence_sha256,
                                     "effective_hash": record.effective_hash,
                                     "adjustments": receipt["study"]["binding"]["adjustments"]})
    journal.checkpoint("status", "candidate_frozen")
    return current(state, directory, plan, journal)


def verify_frozen(lease, directory, plan, journal, expected_revision=None):
    state = StudyState(lease, plan.experiment_id)
    check_revision(state, directory, plan, journal, expected_revision)
    candidate = journal.checkpoint("candidate")
    record = state.latest
    if not candidate or record is None or candidate["run_id"] != record.run_id or record.status != "reviewed":
        raise Invalid("confirmation", "nenhum candidato revisado e congelado")
    record = reconcile_round(lease, state, record)
    receipt = read_managed_receipt(lease, record)
    binding = receipt["study"]["binding"]
    if (not state.closed or journal.checkpoint("managed_stop") != "target_met" or receipt["verdict"] != "passed"
            or candidate["effective_hash"] != record.effective_hash
            or candidate["evidence_sha256"] != record.evidence_sha256
            or candidate["adjustments"] != binding["adjustments"]):
        raise Invalid("confirmation", "candidato congelado não confere com a evidência revisada")
    return candidate


def retry_round(lease, directory, plan, journal, path, *, data_root=None, expected_revision=None):
    state = StudyState(lease, plan.experiment_id)
    payload = check_revision(state, directory, plan, journal, expected_revision)
    action = payload["next_action"]
    if not action or action["id"] != "retry_round":
        raise Invalid("study.retry", "nova tentativa só após falha comprovada com orçamento restante")
    base = valid(validate_proposal(lease.workspace, lease.proposal_id))
    study = load_study(path, base.definition, experiment_id=plan.experiment_id)
    if data_root is None:
        data_root = journal.checkpoint("managed_data_root")
    dispatch_study(lease, state, directory.name, base, study, data_root=data_root,
                   confirmation_window=plan.confirmation_window, plan_hash=plan.plan_hash, retry=True)
    return current(state, directory, plan, journal)
