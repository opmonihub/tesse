"""Discoverable contracts for the score/array primitives (not Forge graph nodes)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType

from .features import contrast_groups, orient_columns
from .ranking import apply_edge_rank, calibrate_edge_rank, rank_cutoff

_MISSING = object()


@dataclass(frozen=True)
class Parameter:
    type: str
    parse: Callable
    default: object = _MISSING
    minimum: int | None = None
    choices: tuple = ()
    optional: bool = False


@dataclass(frozen=True)
class Primitive:
    key: str
    description: str
    inputs: Mapping
    outputs: Mapping
    parameters: Mapping[str, Parameter]
    callables: Mapping[str, Callable]
    timing: str
    rules: tuple[str, ...]
    validate: Callable | None = None
    warmup: int = 0

    def __post_init__(self):
        for name in ("inputs", "outputs", "parameters", "callables"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def category(self):
        return self.key.split(".", 1)[0]

    @property
    def version(self):
        return self.key.split("@", 1)[1]

    def resolve(self, values: Mapping) -> dict:
        if not isinstance(values, Mapping):
            raise ValueError("primitive parameters must be a mapping")
        if unknown := values.keys() - self.parameters.keys():
            raise ValueError(f"unknown primitive parameters: {unknown}")
        result = {}
        for name, parameter in self.parameters.items():
            value = values.get(name, parameter.default)
            if value is _MISSING:
                if parameter.optional:
                    continue
                raise ValueError(f"missing parameter: {name}")
            result[name] = parameter.parse(value, name)
        if self.validate is not None:
            self.validate(result)
        return result


def _indices(value, name):
    if (not isinstance(value, (list, tuple)) or not value
            or any(type(index) is not int or index < 0 for index in value)):
        raise ValueError(f"{name} requires nonempty, nonnegative column indices")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} has duplicate indices")
    return tuple(value)


def _groups(value, name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} requires positive/negative column pairs")
    pairs = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"{name} requires positive/negative column pairs")
        positive, negative = (_indices(indices, name) for indices in pair)
        if set(positive) & set(negative):
            raise ValueError(f"{name} sides must be disjoint")
        pairs.append((positive, negative))
    return tuple(pairs)


def _neutral(value, name):
    if value not in ("zero", "preserve"):
        raise ValueError(f"{name} must be 'zero' or 'preserve'")
    return value


def _int(value, name, *, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _rank(minimum):
    return Parameter("int", partial(_int, minimum=minimum), minimum=minimum)


def _rank_order(values):
    if not values["rank_core"] < values["rank_budget"] < values["rank_pool"]:
        raise ValueError("expected rank_core < rank_budget < rank_pool")


PRIMITIVES = MappingProxyType({contract.key: contract for contract in (
    Primitive(
        "features.directional_transforms@1.0.0",
        "Column orientation and probability-group contrasts on aligned arrays.",
        {"values": "float[n,m]", "side": "int[n]"},
        {"oriented": "float[n,m]", "contrasts": "float[n,k]"},
        {"columns": Parameter("list[int]", _indices, optional=True),
         "groups": Parameter("list[[list[int], list[int]]]", _groups, optional=True),
         "neutral": Parameter("enum", _neutral, "zero", choices=("zero", "preserve"))},
        {"oriented": orient_columns, "contrasts": contrast_groups},
        "Row-local; available with the aligned row and side, without future values.",
        ("side values are -1, 0 or 1", "indices are valid for the input width and unique",
         "positive and negative groups are disjoint"),
    ),
    Primitive(
        "selection.rank_cutoff@1.0.0", "Strict descending rank cutoff.",
        {"scores": "float[n]"}, {"cutoff": "float"}, {"rank": _rank(1)},
        {"cutoff": rank_cutoff}, "Available after the supplied score window closes.",
        ("scores are finite, one-dimensional and longer than rank", "selection uses > cutoff"),
    ),
    Primitive(
        "selection.edge_rank@1.0.0", "Estimator-independent calibrated marginal rank selection.",
        {"calibration": ("base_scores", "secondary_scores"),
         "evaluation": ("base_scores", "secondary_scores", "calibration: EdgeRankCalibration")},
        {"calibration": "EdgeRankCalibration", "evaluation": "bool[n]"},
        {"rank_pool": _rank(2), "rank_core": _rank(1), "rank_budget": _rank(2)},
        {"calibration": calibrate_edge_rank, "evaluation": apply_edge_rank},
        "Fit on calibration scores only; evaluation applies frozen cutoffs without refitting.",
        ("rank_core < rank_budget < rank_pool", "strict secondary-then-base tie breaking"),
        _rank_order,
    ),
)})


def get_primitive(key: str) -> Primitive:
    """Resolve an exact primitive key, independently of the Forge node registry."""
    return PRIMITIVES[key]
