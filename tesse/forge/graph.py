"""Resolve only registered components and validate all wiring before execution."""

from dataclasses import replace

from .catalog import COMPONENTS, VOLATILITY_SCORE
from .components import VOLATILITY_PERIODS
from .contracts import Invalid, Node, keys, mapping, slug

XGB_LEARNER = "learner.xgboost@1.0.0"
LEARNERS = {"learner.hgb@1.0.0", XGB_LEARNER}


def uses_xgboost(experiment):
    return any(node.component == XGB_LEARNER for node in experiment.nodes)


def _route_issue(experiment, *, standalone_study):
    xgboost = uses_xgboost(experiment)
    standalone_development = standalone_study and experiment.managed_lineage is not None
    if (any(node.component == VOLATILITY_SCORE for node in experiment.nodes)
            and not (xgboost and standalone_development)):
        return (
            VOLATILITY_SCORE,
            "volatilidade exige XGBoost em run --study avulso gerenciado de desenvolvimento",
        )
    if xgboost and not standalone_development:
        return "learner.xgboost", "XGBoost exige run --study avulso gerenciado de desenvolvimento"
    return None


def require_supported_route(experiment, *, standalone_study=False):
    if issue := _route_issue(experiment, standalone_study=standalone_study):
        raise Invalid(*issue)


def require_viable_route(experiment, field):
    """Reject a document only if neither route passes the component policy."""
    if _route_issue(experiment, standalone_study=False) is not None:
        if issue := _route_issue(experiment, standalone_study=True):
            raise Invalid(field, f"experimento sem rota suportada: {issue[1]}")


def _reference(value, field, nodes, expected):
    parts = value.split(".") if isinstance(value, str) else []
    if len(parts) != 2 or parts[0] not in nodes:
        raise Invalid(field, "referência inválida: use nó.saída de um nó existente")
    source, output = parts
    produced = COMPONENTS[nodes[source].component].outputs.get(output)
    if produced is None:
        raise Invalid(field, "porta de saída inexistente")
    if produced != expected:
        raise Invalid(field, f"porta incompatível: esperado {expected}, recebido {produced}")
    return source, output


def _order(nodes, result, field):
    visiting, done, ordered = set(), set(), []

    def visit(node_id):
        if node_id in visiting:
            raise Invalid(f"{field}.{node_id}", "ciclo no grafo")
        if node_id in done:
            return
        visiting.add(node_id)
        for source, _ in sorted(nodes[node_id].inputs.values()):
            visit(source)
        visiting.remove(node_id)
        done.add(node_id)
        ordered.append(nodes[node_id])

    visit(result[0])
    unused = nodes.keys() - done
    if unused:
        unrelated = sorted(unused)[0]
        raise Invalid(f"{field}.{unrelated}", "nó não contribui para result")
    return tuple(ordered)


def _cross_check(nodes, ordered, field):
    # Track the feature count and the temporal partition behind each fitted model.
    columns, training_origin = {}, {}
    for node in ordered:
        prefix = f"{field}.{node.id}"
        kind = node.component.split("@")[0]

        def upstream(port):
            return nodes[node.inputs[port][0]]

        if kind == "score.directional_return" or node.component == VOLATILITY_SCORE:
            features = upstream("features")
            periods = features.params["periods"]
            if node.component == VOLATILITY_SCORE and (
                    features.component != "features.returns@1.0.0" or tuple(periods) != VOLATILITY_PERIODS):
                raise Invalid(f"{prefix}.inputs.features", "volatilidade exige features.returns@1.0.0 com [1, 5, 15]")
            if node.params["period"] not in periods:
                raise Invalid(f"{prefix}.params.period", "período ausente no nó de features")
            columns[node.id] = len(periods) + 1 + int(node.component == VOLATILITY_SCORE)
        elif kind == "split.temporal":
            if upstream("labels").inputs["scores"] != node.inputs["scores"]:
                raise Invalid(f"{prefix}.inputs.labels", "rótulos precisam vir do mesmo score")
            columns[node.id] = columns[upstream("scores").id]
            training_origin[node.id] = node.id
        elif kind == "selection.edge_training" or node.component in LEARNERS:
            if node.component == XGB_LEARNER and upstream("train").component != "split.temporal@1.0.0":
                raise Invalid(f"{prefix}.inputs.train", "XGBoost exige a porta train do split, sem filtro marginal")
            train = upstream("train").id
            columns[node.id] = columns[train]
            training_origin[node.id] = training_origin[train]
            monotonic = node.params.get("monotonic_cst")
            if monotonic is not None and len(monotonic) != columns[train]:
                raise Invalid(f"{prefix}.params.monotonic_cst", f"esperadas {columns[train]} colunas")
        if kind in {"predict.hgb", "predict.xgboost", "selection.raw_rank", "selection.edge_rank"}:
            source = upstream("calibration").id
            if source != upstream("evaluation").id:
                raise Invalid(f"{prefix}.inputs.evaluation", "avaliação e calibração precisam do mesmo nó de origem")
            if kind.startswith("predict."):
                model = upstream("model")
                if model.component != node.component.replace("predict.", "learner.", 1):
                    raise Invalid(f"{prefix}.inputs.model", "learner e preditor precisam corresponder")
                if training_origin[model.id] != source:
                    raise Invalid(f"{prefix}.inputs.model", "modelo ajustado em outra trilha de features")
        if kind == "report.compare" and node.inputs["control"] == node.inputs["candidate"]:
            raise Invalid(f"{prefix}.inputs.candidate", "use um replay distinto do controle")


def compile_graph(raw, result, field, output_type):
    raw = mapping(raw, field)
    if not 1 <= len(raw) <= 128:
        raise Invalid(field, "esperado grafo de 1 a 128 nós")
    nodes = {}
    for node_id, value in raw.items():
        prefix = f"{field}.{node_id}"
        slug(node_id, prefix)
        value = keys(value, prefix, required=("component",), optional=("inputs", "params"))
        component = value["component"]
        if not isinstance(component, str) or component not in COMPONENTS:
            raise Invalid(f"{prefix}.component", "componente versionado não registrado")
        contract = COMPONENTS[component]
        params = contract.resolve(value.get("params", {}), f"{prefix}.params")
        keys(value.get("inputs", {}), f"{prefix}.inputs", required=contract.inputs)
        nodes[node_id] = Node(node_id, component, params, {})
    if sum(node.component == "snapshot.m1@1.0.0" for node in nodes.values()) != 1:
        raise Invalid(field, "use exatamente um nó snapshot.m1 por experimento")

    for node_id, node in nodes.items():
        ports = COMPONENTS[node.component].inputs
        nodes[node_id] = replace(node, inputs={
            name: _reference(value, f"{field}.{node_id}.inputs.{name}", nodes, ports[name])
            for name, value in raw[node_id].get("inputs", {}).items()
        })
    output = _reference(result, f"{field.removesuffix('.nodes')}.result.from", nodes, output_type)
    ordered = _order(nodes, output, field)
    _cross_check(nodes, ordered, field)
    return ordered, output


def validate_managed_graph(nodes, result, field):
    """Only the shared causal returns pipeline, with R5 control and a supported candidate."""
    by_id = {node.id: node for node in nodes}

    def require(node, kind):
        if node.component != f"{kind}@1.0.0":
            raise Invalid(field, f"grafo gerenciado exige {kind}, recebeu {node.component}")
        return node

    def source(node, port, kind):
        return require(by_id[node.inputs[port][0]], kind)

    comparison = require(by_id[result[0]], "report.compare")
    control = source(comparison, "control", "replay.option_m1")
    candidate = source(comparison, "candidate", "replay.option_m1")
    raw = source(control, "proposals", "selection.raw_rank")
    edge = source(candidate, "proposals", "selection.edge_rank")
    split = source(raw, "calibration", "split.temporal")
    predictor = by_id[edge.inputs["calibration"][0]]
    if predictor.component == "predict.xgboost@1.0.0":
        training = source(predictor, "model", "learner.xgboost")
    else:
        require(predictor, "predict.hgb")
        fit = source(predictor, "model", "learner.hgb")
        training = source(fit, "train", "selection.edge_training")
    base = by_id[split.inputs["scores"][0]]
    if base.component == VOLATILITY_SCORE:
        if predictor.component != "predict.xgboost@1.0.0":
            raise Invalid(field, "volatilidade exige o par learner/predict XGBoost")
    else:
        require(base, "score.directional_return")
    features = source(base, "features", "features.returns")
    labels = source(split, "labels", "labels.directional")
    snapshot = source(features, "candles", "snapshot.m1")
    shared = ((predictor, "calibration", split), (training, "train", split),
              (labels, "scores", base), (labels, "candles", snapshot),
              (control, "candles", snapshot), (candidate, "candles", snapshot))
    if any(node.inputs[port][0] != expected.id for node, port, expected in shared):
        raise Invalid(field, "os braços precisam compartilhar dados, features e partição")
