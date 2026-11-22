"""Built-in versioned Forge graph contracts and their approved executors."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ..adapters.sklearn_adapter import HGB_KEY
from ..adapters.xgboost_adapter import PARAMETERS as XGB_PARAMETERS
from ..adapters.xgboost_adapter import XGB_KEY
from . import components as impl
from .contracts import Invalid, Window, keys, number

if TYPE_CHECKING:
    from .snapshots import Snapshot

VOLATILITY_SCORE = "score.directional_return_volatility@1.0.0"


@dataclass(frozen=True)
class Parameter:
    default: Any
    parse: Callable[[Any, str], Any]
    type: str
    minimum: int | float | None = None
    maximum: int | float | None = None


@dataclass(frozen=True)
class ComponentContext:
    """Inputs supplied by Python consumers, never paths or code selected by a binding."""

    data_root: Path | None = None
    snapshot: Mapping[str, str] | None = None
    windows: Mapping[str, Window] | None = None
    evaluation: Window | None = None
    criteria: Mapping[str, float] | None = None
    verified_snapshot: "Snapshot | None" = None
    trace_study: bool = False


@dataclass(frozen=True)
class Component:
    inputs: dict[str, str]
    outputs: dict[str, str]
    params: dict[str, Parameter]
    execute: Callable
    description: str
    timing: str
    warmup: int | Callable
    key: str = ""
    requires: tuple[str, ...] = ()
    check: Callable | None = None
    adapter: str | None = None

    def __post_init__(self):
        for name in ("inputs", "outputs", "params"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def category(self):
        return self.key.split(".", 1)[0]

    @property
    def version(self):
        return self.key.split("@", 1)[1]

    def resolve(self, params, field):
        keys(params, field, optional=self.params)
        resolved = {
            name: param.parse(params.get(name, param.default), f"{field}.{name}")
            for name, param in self.params.items()
        }

        if self.check is not None:
            self.check(resolved, field)
        return resolved

    def invoke(self, context, params, **inputs):
        from .snapshots import Snapshot

        ports = {
            "Candles": Snapshot,
            "Features": impl.Features,
            "Scores": impl.Frame,
            "Labels": impl.Labels,
            "Train": impl.Training,
            "Calibration": impl.Frame,
            "Evaluation": impl.Frame,
            "Model": impl.Fitted,
            "ScoredCalibration": impl.Scored,
            "ScoredEvaluation": impl.Scored,
            "Proposals": impl.Proposals,
            "Book": impl.Book,
            "Comparison": impl.Comparison,
        }
        if not isinstance(context, ComponentContext):
            raise Invalid(self.key, "expected ComponentContext")
        context_types = {
            "evaluation": Window,
            "windows": Mapping,
            "criteria": Mapping,
            "snapshot": Mapping,
            "data_root": Path,
        }
        for name in self.requires:
            if not isinstance(getattr(context, name), context_types[name]):
                raise Invalid(self.key, f"missing or invalid context: {name}")
        keys(inputs, self.key, required=self.inputs)
        for port, type_name in self.inputs.items():
            if not isinstance(inputs[port], ports[type_name]):
                raise Invalid(f"{self.key}.{port}", f"expected {type_name}")
        result = self.execute(context, params, **inputs)
        if result.keys() != self.outputs.keys() or any(
            not isinstance(result[port], ports[type_name])
            for port, type_name in self.outputs.items()
        ):
            raise Invalid(self.key, "invalid component outputs")
        return result


def _ranks(params, field):
    core, pool = params["rank_core"], params["rank_pool"]
    budget = params.get("rank_budget")
    if not core < pool or (budget is not None and not core < budget < pool):
        raise Invalid(
            field,
            "esperado rank_core < rank_pool e, se houver, rank_core < rank_budget < rank_pool",
        )


def int_param(default, low=1, high=1000000):
    return Parameter(default, partial(number, low=low, high=high, integer=True), "int", low, high)


def float_param(default, low=0, high=1000):
    return Parameter(default, partial(number, low=low, high=high), "number", low, high)


def periods(value, field):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 32:
        raise Invalid(field, "esperada lista de 1 a 32 períodos")
    result = [
        number(p, f"{field}.{i}", low=1, high=1440, integer=True) for i, p in enumerate(value)
    ]
    if len(set(result)) != len(result):
        raise Invalid(field, "períodos duplicados")
    return result


def monotonic(value, field):
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 33:
        raise Invalid(field, "esperada lista de restrições em {-1, 0, 1}")
    return [number(v, f"{field}.{i}", low=-1, high=1, integer=True) for i, v in enumerate(value)]


def _snapshot(context):
    # snapshots uses validation helpers, so load it only at execution time.
    from .snapshots import load_snapshot

    snapshot = context.verified_snapshot or load_snapshot(context.data_root, context.snapshot)
    if any(
        w.start < snapshot.span.start or w.end > snapshot.span.end for w in context.windows.values()
    ):
        raise Invalid("snapshot", "dados não cobrem as janelas configuradas")
    return snapshot


def _replay(context, _, candles, proposals):
    options = {}
    if context.trace_study:
        options["calibration_window"] = (context.windows or {}).get("calibration")
    return {"book": impl.replay(candles, proposals, context.evaluation, **options)}


def component_registry(entries):
    result = {}
    for key, component in entries:
        if key in result:
            raise ValueError(f"componente duplicado: {key}")
        if not callable(component.execute):
            raise ValueError(f"componente sem executor: {key}")
        result[key] = replace(component, key=key)
    return MappingProxyType(result)


COMPONENTS = component_registry(
    (
        (
            "snapshot.m1@1.0.0",
            Component(
                {},
                {"candles": "Candles"},
                {},
                execute=lambda c, _: {"candles": _snapshot(c)},
                description="Read and verify the pinned M1 snapshot",
                requires=("data_root", "snapshot", "windows"),
                timing="Original closed bars, never the latest mutable cache",
                warmup=0,
            ),
        ),
        (
            "features.returns@1.0.0",
            Component(
                {"candles": "Candles"},
                {"features": "Features"},
                {"periods": Parameter((1, 5, 15), periods, "list[int]", 1, 1440)},
                execute=lambda _, p, candles: {"features": impl.returns(candles.bars, **p)},
                description="Compute lagged returns",
                timing="Only the current and previous candles",
                warmup=lambda p: max(p["periods"]),
            ),
        ),
        (
            "score.directional_return@1.0.0",
            Component(
                {"features": "Features"},
                {"scores": "Scores"},
                {"period": int_param(5, high=1440)},
                execute=lambda _, p, features: {"scores": impl.directional(features, **p)},
                description="Score directional returns",
                timing="Available when the configured feature window closes",
                warmup=0,
            ),
        ),
        (
            VOLATILITY_SCORE,
            Component(
                {"features": "Features"},
                {"scores": "Scores"},
                {"period": int_param(impl.VOLATILITY_SIDE_PERIOD, 5, 5)},
                execute=lambda _, p, features: {"scores": impl.directional_volatility(features, **p)},
                description="Keep R5 and append a trailing, non-directional volatility feature",
                timing="Uses only the 15 one-bar returns available after the anchor candle closes",
                warmup=0,  # The required upstream already has R15 warmup.
            ),
        ),
        (
            "labels.directional@1.0.0",
            Component(
                {"candles": "Candles", "scores": "Scores"},
                {"labels": "Labels"},
                {},
                execute=lambda _, p, candles, scores: {
                    "labels": impl.label_direction(candles.bars, scores)
                },
                description="Label the directional outcome",
                timing="Outcome matures at label time; never a decision feature",
                warmup=0,
            ),
        ),
        (
            "split.temporal@1.0.0",
            Component(
                {"scores": "Scores", "labels": "Labels"},
                {"train": "Train", "calibration": "Calibration", "evaluation": "Evaluation"},
                {},
                execute=lambda c, p, scores, labels: impl.split(scores, labels, c.windows),
                description="Partition rows by temporal windows",
                requires=("windows",),
                timing="Chronological split; labels must be mature within each window",
                warmup=0,
            ),
        ),
        (
            "selection.edge_training@1.0.0",
            Component(
                {"train": "Train"},
                {"train": "Train"},
                {"rank_pool": int_param(100), "rank_core": int_param(14)},
                execute=lambda _, p, train: {"train": impl.train_edges(train, **p)},
                description="Select marginal training rows",
                check=_ranks,
                timing="Cutoffs use training data only",
                warmup=0,
            ),
        ),
        (
            "learner.hgb@1.0.0",
            Component(
                {"train": "Train"},
                {"model": "Model"},
                {
                    "max_iter": int_param(100, high=10000),
                    "learning_rate": float_param(0.05, 1e-9, 10),
                    "max_leaf_nodes": int_param(7, low=2),
                    "min_samples_leaf": int_param(30),
                    "l2_regularization": float_param(10),
                    "random_state": int_param(0, 0, 2**32 - 1),
                    "max_bins": int_param(255, 2, 255),
                    "monotonic_cst": Parameter(None, monotonic, "list[int] | null", -1, 1),
                },
                execute=lambda _, p, train: {"model": impl.fit_hgb(train, p)},
                description="Fit the HGB estimator",
                adapter=HGB_KEY,
                timing="Fit once on the mature training partition",
                warmup=0,
            ),
        ),
        (
            "predict.hgb@1.0.0",
            Component(
                {"model": "Model", "calibration": "Calibration", "evaluation": "Evaluation"},
                {"calibration": "ScoredCalibration", "evaluation": "ScoredEvaluation"},
                {},
                execute=lambda _, p, model, calibration, evaluation: impl.predict_hgb(
                    model, calibration, evaluation
                ),
                description="Score with the fitted HGB estimator",
                adapter=HGB_KEY,
                timing="Uses the frozen model, preserving chronological partitions",
                warmup=0,
            ),
        ),
        (
            "learner.xgboost@1.0.0",
            Component(
                {"train": "Train"}, {"model": "Model"},
                {name: (int_param if type(default) is int else float_param)(default, low, high)
                 for name, (default, low, high) in XGB_PARAMETERS.items()},
                execute=lambda _, p, train: {"model": impl.fit_xgboost(train, p)},
                description="Fit XGBoost in CPU on every eligible training row",
                adapter=XGB_KEY,
                timing="Fit once on the complete mature training partition",
                warmup=0,
            ),
        ),
        (
            "predict.xgboost@1.0.0",
            Component(
                {"model": "Model", "calibration": "Calibration", "evaluation": "Evaluation"},
                {"calibration": "ScoredCalibration", "evaluation": "ScoredEvaluation"},
                {},
                execute=lambda _, p, model, calibration, evaluation: impl.predict_model(model, calibration, evaluation),
                description="Score WIN of the proposed side using frozen XGBoost probabilities",
                adapter=XGB_KEY,
                timing="Uses the frozen model and preserves chronological partitions",
                warmup=0,
            ),
        ),
        (
            "selection.raw_rank@1.0.0",
            Component(
                {"calibration": "Calibration", "evaluation": "Evaluation"},
                {"proposals": "Proposals"},
                {"rank_budget": int_param(38)},
                execute=lambda c, p, calibration, evaluation: {
                    "proposals": impl.raw_rank(calibration, evaluation, trace_study=c.trace_study, **p)
                },
                description="Select using the raw-score cutoff",
                timing="Calibration window fixes a strict cutoff before evaluation",
                warmup=0,
            ),
        ),
        (
            "selection.edge_rank@1.0.0",
            Component(
                {"calibration": "ScoredCalibration", "evaluation": "ScoredEvaluation"},
                {"proposals": "Proposals"},
                {
                    "rank_pool": int_param(100),
                    "rank_core": int_param(14),
                    "rank_budget": int_param(38),
                },
                execute=lambda c, p, calibration, evaluation: {
                    "proposals": impl.edge_rank(calibration, evaluation, trace_study=c.trace_study, **p)
                },
                description="Select a protected core and scored margin",
                check=_ranks,
                timing="Calibration fixes all cutoffs before evaluation",
                warmup=0,
            ),
        ),
        (
            "selection.dmi_cross@1.0.0",
            Component(
                {"candles": "Candles"},
                {"proposals": "Proposals"},
                {
                    "dmi_period": int_param(14, 2, 100),
                    "adx_smoothing": int_param(14, 2, 100),
                    "min_adx": float_param(25, high=100),
                },
                execute=lambda c, p, candles: {
                    "proposals": impl.dmi_proposals(candles, c.evaluation, **p),
                },
                description="Emit DMI crossing proposals",
                requires=("evaluation",),
                timing="Causal updates on closed bars; emits only in the evaluation window",
                warmup=lambda p: p["dmi_period"] + p["adx_smoothing"],
            ),
        ),
        (
            "replay.option_m1@1.0.0",
            Component(
                {"candles": "Candles", "proposals": "Proposals"},
                {"book": "Book"},
                {},
                execute=_replay,
                description="Replay nonoverlapping M1 options",
                requires=("evaluation",),
                timing="Reserves before outcome is observable",
                warmup=0,
            ),
        ),
        (
            "report.compare@1.0.0",
            Component(
                {"control": "Book", "candidate": "Book"},
                {"comparison": "Comparison"},
                {},
                execute=lambda c, p, control, candidate: {
                    "comparison": impl.compare(
                        control,
                        candidate,
                        c.evaluation,
                        c.criteria,
                    ),
                },
                description="Compare two replay books",
                requires=("evaluation", "criteria"),
                timing="Uses settled outcomes after the evaluation window",
                warmup=0,
            ),
        ),
    )
)
