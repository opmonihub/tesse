"""Execute a compiled graph exactly once per node and publish its final result."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..primitives.ranking import apply_edge_rank
from . import components as impl
from .catalog import COMPONENTS, ComponentContext
from .contracts import Invalid
from .evidence import publish_run
from .graph import LEARNERS, require_supported_route, uses_xgboost
from .guard import proposal_guard
from .partitions import record_development_exposure, verify_managed_span
from .snapshots import Snapshot, load_snapshot, read_snapshot_manifest
from .study import Study
from .validation import validate_proposal


@dataclass(frozen=True)
class Execution:
    data_root: Path
    snapshot: Snapshot
    output: impl.Comparison | impl.Scored
    training: dict
    state: dict = field(default_factory=dict)


def execute_experiment(experiment, data_root, *, verified_snapshot=None, trace_study=False):
    """Execute an already-compiled experiment without writing any artifacts."""
    require_supported_route(experiment, standalone_study=trace_study)
    context = ComponentContext(data_root, experiment.snapshot, experiment.windows,
                               experiment.windows.get("evaluation"), experiment.criteria, verified_snapshot, trace_study=trace_study)
    outputs, training = {}, {}
    snapshot = None
    for node in experiment.nodes:
        inputs = {key: outputs[source][port] for key, (source, port) in node.inputs.items()}
        contract = COMPONENTS[node.component]
        try:
            result = contract.execute(context, node.resolved_parameters(), **inputs)
        except (ValueError, OSError, EOFError, ImportError, RuntimeError) as exc:
            raise Invalid(f"nodes.{node.id}", str(exc)) from exc
        if set(result) != set(contract.outputs):
            raise Invalid(f"nodes.{node.id}", "componente retornou portas diferentes do contrato")
        outputs[node.id] = result
        if node.component == "snapshot.m1@1.0.0":
            snapshot = result["candles"]
        if node.component in LEARNERS:
            training[node.id] = result["model"].receipt
    node, port = experiment.result
    output = outputs[node][port]
    expected = {"comparison": impl.Comparison, "predictions": impl.Scored}[experiment.result_kind]
    if not isinstance(output, expected):
        raise Invalid("result", "resultado executado não corresponde ao tipo validado")
    state = {**_freeze_execution_state(experiment, outputs), "training": dict(training)}
    return Execution(data_root, snapshot, output, training, state)


@dataclass(frozen=True)
class StudyExecution:
    study: Study
    blocks: Mapping[str, Execution]

    @property
    def verdict(self):
        return "passed" if all(block.output.metrics["meets_study_criteria"] for block in self.blocks.values()) else "failed"


def preflight_study(experiment, study, data_root, *, confirmation_window=None):
    if experiment.managed_lineage is None or experiment.id != study.experiment:
        raise Invalid("study.experiment", "estudo não corresponde ao experimento gerenciado")
    require_supported_route(experiment, standalone_study=confirmation_window is None)
    if uses_xgboost(experiment):
        impl.resolve_xgboost()
    manifest = read_snapshot_manifest(data_root, experiment.snapshot)
    verify_managed_span(manifest, experiment.managed_lineage, experiment.windows, "protocol.windows")
    for block in study.blocks:
        verify_managed_span(manifest, experiment.managed_lineage, block.windows, f"study.blocks.{block.id}")
    if confirmation_window is not None and confirmation_window.start < manifest.span.end:
        raise Invalid("confirmation.window", "confirmação precisa ocorrer após o fim do snapshot de desenvolvimento")
    return manifest


def execute_study(lease, experiment, study, data_root, *, confirmation_window=None, authorized_manifest=None):
    """Evaluate each disjoint fold with fresh typed graph state and one pinned snapshot."""
    lease.active_directory()
    require_supported_route(experiment, standalone_study=confirmation_window is None)
    if lease.proposal_id != study.proposal:
        raise Invalid("study.proposal", "estudo não corresponde à proposta sob a trava")
    manifest = authorized_manifest
    if manifest is None:
        manifest = preflight_study(experiment, study, data_root, confirmation_window=confirmation_window)
        record_development_exposure(lease, experiment, manifest)
    snapshot = load_snapshot(data_root, experiment.snapshot, manifest=manifest)
    blocks = {}
    for block in study.blocks:
        effective = replace(experiment, windows=block.windows)
        try:
            blocks[block.id] = execute_experiment(effective, data_root, verified_snapshot=snapshot, trace_study=True)
        except Invalid as exc:
            raise Invalid(f"study.blocks.{block.id}.{exc.field}", exc.message) from exc
    return StudyExecution(study, blocks)


def _freeze_execution_state(experiment, outputs):
    """Capture only in-memory development state needed by confirmation."""
    models, raw_cutoffs, edge_calibrations = {}, {}, {}
    nodes = {node.id: node for node in experiment.nodes}
    for node in experiment.nodes:
        kind = node.component.split("@")[0]
        if node.component in LEARNERS:
            models[node.id] = outputs[node.id]["model"]
        elif kind == "selection.raw_rank":
            raw_cutoffs[node.id] = outputs[node.id]["proposals"].rank_fit
        elif kind == "selection.edge_rank":
            edge_calibrations[node.id] = outputs[node.id]["proposals"].rank_fit
    return {"models": models, "raw_cutoffs": raw_cutoffs,
            "edge_calibrations": edge_calibrations, "nodes": nodes}


def execute_prepared_comparison(experiment, data_root, prepared, *, verified_manifest=None):
    """Score a final evaluation window using a frozen development state.

    This path never calls ``execute_experiment`` and therefore cannot fit,
    calibrate, or select on the final payload.  It computes only causal feature
    values, applies frozen model/cutoff state, replays the two arms, and
    compares them.
    """
    require_supported_route(experiment)
    if experiment.result_kind != "comparison":
        raise Invalid("confirmation", "confirmação exige experimento de comparação")
    snapshot_node = next((node for node in experiment.nodes if node.component.startswith("snapshot.")), None)
    features_node = next((node for node in experiment.nodes if node.component.startswith("features.returns")), None)
    score_node = next((node for node in experiment.nodes if node.component.startswith("score.directional_return")), None)
    labels_node = next((node for node in experiment.nodes if node.component.startswith("labels.directional")), None)
    if not all((snapshot_node, features_node, score_node, labels_node)):
        raise Invalid("confirmation", "grafo não contém a trilha causal v2 esperada")
    snapshot = (load_snapshot(data_root, experiment.snapshot, manifest=verified_manifest)
                if verified_manifest is not None else load_snapshot(data_root, experiment.snapshot))
    evaluation_window = experiment.windows["evaluation"]
    if evaluation_window.start < snapshot.span.start or evaluation_window.end > snapshot.span.end:
        raise Invalid("confirmation", "payload final não cobre a janela de avaliação")
    features = impl.returns(snapshot.bars, **features_node.resolved_parameters())
    scores = impl.directional(features, **score_node.resolved_parameters())
    labels = impl.label_direction(snapshot.bars, scores)
    del labels  # labels are consumed by replay; no final calibration uses them.
    start, end = int(evaluation_window.start.timestamp()), int(evaluation_window.end.timestamp())
    evaluation = scores.where((scores.anchors >= start) & (scores.anchors < end))
    # Compute final probabilities only with the already-fitted development model.
    probabilities = {}
    for node in experiment.nodes:
        if node.component.startswith("predict.hgb"):
            model_id = node.inputs["model"][0]
            fitted = prepared.get("models", {}).get(model_id)
            if fitted is None:
                raise Invalid("confirmation", "modelo ajustado de desenvolvimento ausente")
            probabilities[node.id] = impl.predict_hgb(fitted, evaluation, evaluation)["evaluation"].probabilities
    proposals = {}
    for node in experiment.nodes:
        kind = node.component.split("@")[0]
        if kind == "selection.raw_rank":
            cutoff = prepared.get("raw_cutoffs", {}).get(node.id)
            if cutoff is None:
                raise Invalid("confirmation", f"corte congelado ausente para {node.id}")
            mask = evaluation.raw > cutoff
            proposals[node.id] = impl.Proposals(evaluation.anchors[mask], evaluation.sides[mask])
        elif kind == "selection.edge_rank":
            calibration = prepared.get("edge_calibrations", {}).get(node.id)
            if calibration is None:
                raise Invalid("confirmation", f"calibração congelada ausente para {node.id}")
            source = node.inputs["calibration"][0]
            secondary = probabilities.get(source)
            if secondary is None:
                # The prediction node may be referenced by its output port.
                secondary = next(iter(probabilities.values()), None)
            if secondary is None:
                raise Invalid("confirmation", "probabilidades congeladas ausentes")
            mask = apply_edge_rank(evaluation.raw, secondary, calibration)
            proposals[node.id] = impl.Proposals(evaluation.anchors[mask], evaluation.sides[mask])
    books = {}
    for node in experiment.nodes:
        if node.component.startswith("replay.option_m1"):
            source = node.inputs["proposals"][0]
            if source not in proposals:
                raise Invalid("confirmation", f"propostas congeladas ausentes para {source}")
            books[node.id] = impl.replay(snapshot, proposals[source], evaluation_window)
    compare_node = next((node for node in experiment.nodes if node.component.startswith("report.compare")), None)
    if compare_node is None:
        raise Invalid("confirmation", "comparação final ausente")
    control = books.get(compare_node.inputs["control"][0])
    candidate = books.get(compare_node.inputs["candidate"][0])
    if control is None or candidate is None:
        raise Invalid("confirmation", "braços de comparação ausentes")
    output = impl.compare(control, candidate, evaluation_window, experiment.criteria)
    return Execution(Path(data_root), snapshot, output, prepared.get("training", {}), prepared)


def run(root, proposal_id, experiment_id, *, data_root=None, study_path=None, export_research_model=False):
    """Validate and execute the current active proposal under one workspace lease."""
    with proposal_guard(root, proposal_id) as lease:
        validation = validate_proposal(lease.workspace, proposal_id)
        if not validation.valid():
            issue = validation.issues[0]
            raise Invalid(issue.path, issue.message)
        experiment = validation.definition.experiments.get(experiment_id)
        if experiment is None:
            raise Invalid("protocol.yaml", f"experimento não encontrado: {experiment_id}")
        if export_research_model and (study_path is None or experiment.managed_lineage is None
                                      or not uses_xgboost(experiment)):
            raise Invalid("--export-research-model", "exige XGBoost com run --study avulso gerenciado")
        require_supported_route(experiment, standalone_study=study_path is not None)
        from .study_state import StudyState

        state = StudyState(lease, experiment_id)
        if experiment.managed_lineage is not None or state.exists or study_path is not None:
            from .managed import run_standalone

            return run_standalone(lease, validation, experiment, state, study_path, data_root=data_root,
                                  export_research_model=export_research_model)
        execution = execute_experiment(experiment, lease.workspace.effective_data_root(data_root))
        return publish_run(lease, validation, experiment, execution)
