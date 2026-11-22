"""Shared, inspectable calculation contracts used by configuration and help.

The registry contains reusable observations only. Entry and exit rules belong to
the declarative configuration, and calculation functions remain usable alone.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Callable

from .indicators import dmi, ema
from .indicators.supertrend import supertrend
from .indicators.talib_sma import atr, bollinger, macd, obv, rsi, sma, stochastic
from .patterns.candles import candle_pattern, engulfing, pin_reversal
from .patterns.market import breakout_retest, failed_breakout
from .patterns.price_action import hikkake, inside_breakout

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_CATEGORIES = {"indicators", "candles", "price_action", "market"}
_INPUTS = {"timestamp", "open", "high", "low", "close", "volume"}
_TYPES = {"int", "number", "bool", "string"}
_OUTPUT_TYPES = {"number", "int", "bool", "string", "bias"}


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: str
    default: object
    description: str = "Component calculation parameter."
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("Nome de parametro invalido")
        if (not isinstance(self.type, str) or self.type not in _TYPES
                or not isinstance(self.description, str) or not self.description.strip()):
            raise ValueError(f"Metadados de parametro invalidos: {self.name}")
        for bound in (self.minimum, self.maximum):
            if bound is not None and (
                type(bound) not in (int, float) or not math.isfinite(bound)
            ):
                raise ValueError(f"Limite de parametro invalido: {self.name}")
        if (self.minimum is not None and self.maximum is not None
                and self.minimum > self.maximum):
            raise ValueError(f"Limites invertidos: {self.name}")
        if (self.minimum is not None or self.maximum is not None) and self.type not in {"int", "number"}:
            raise ValueError(f"Limite numerico em parametro nao numerico: {self.name}")
        if not isinstance(self.choices, tuple):
            raise ValueError(f"Opcoes devem ser imutaveis: {self.name}")
        for choice in self.choices:
            self.validate(choice)
        if len(set(self.choices)) != len(self.choices):
            raise ValueError(f"Opcoes duplicadas: {self.name}")
        self.validate(self.default)

    def validate(self, value):
        types = {"int": (int,), "number": (int, float), "bool": (bool,), "string": (str,)}
        if type(value) not in types[self.type]:
            raise ValueError(f"params.{self.name}: esperado {self.type}")
        if type(value) is float and not math.isfinite(value):
            raise ValueError(f"params.{self.name}: numero nao finito")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"params.{self.name}: minimo {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"params.{self.name}: maximo {self.maximum}")
        if self.choices and value not in self.choices:
            raise ValueError(f"params.{self.name}: opcao desconhecida")
        return value

    def describe(self):
        result = {"type": self.type, "default": self.default, "description": self.description}
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        return result


@dataclass(frozen=True)
class OutputSpec:
    name: str
    type: str
    description: str
    choices: tuple = ()

    def __post_init__(self):
        if (not isinstance(self.name, str) or not _NAME.fullmatch(self.name)
                or not isinstance(self.type, str) or self.type not in _OUTPUT_TYPES
                or not isinstance(self.description, str)
                or not self.description or not isinstance(self.choices, tuple)):
            raise ValueError("Metadados de saida invalidos")
        allowed = {"number": (int, float), "int": (int,), "bool": (bool,),
                   "string": (str,), "bias": (str,)}[self.type]
        if any(type(value) not in allowed or
               (type(value) is float and not math.isfinite(value)) for value in self.choices):
            raise ValueError("Opcoes de saida invalidas")
        if self.type == "bias" and set(self.choices) != {"bullish", "bearish", "none"}:
            raise ValueError("Bias deve documentar bullish, bearish e none")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("Opcoes de saida duplicadas")

    def describe(self):
        result = {"type": self.type, "description": self.description}
        if self.choices:
            result["choices"] = list(self.choices)
        return result


@dataclass(frozen=True)
class ComponentDefinition:
    id: str
    category: str
    version: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[OutputSpec, ...]
    parameters: tuple[ParameterSpec, ...]
    warmup: str
    initialization: str
    timing: str
    calculator: Callable

    def __post_init__(self):
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("ID de componente deve ser qualificado")
        if self.category not in _CATEGORIES or not self.id.startswith(self.category + "."):
            raise ValueError("Categoria de componente invalida")
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ValueError("Versao de componente invalida")
        for name in ("description", "warmup", "initialization", "timing"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"Metadado obrigatorio: {name}")
        if (not isinstance(self.inputs, tuple) or not self.inputs
                or any(name not in _INPUTS for name in self.inputs)
                or len(set(self.inputs)) != len(self.inputs)):
            raise ValueError("Entradas de componente invalidas")
        if (not isinstance(self.outputs, tuple) or not self.outputs
                or any(not isinstance(o, OutputSpec) for o in self.outputs)
                or len({o.name for o in self.outputs}) != len(self.outputs)):
            raise ValueError("Saidas de componente invalidas")
        if (not isinstance(self.parameters, tuple)
                or any(not isinstance(p, ParameterSpec) for p in self.parameters)
                or len({p.name for p in self.parameters}) != len(self.parameters)):
            raise ValueError("Parametros de componente invalidos")
        if not callable(self.calculator):
            raise ValueError("Calculo de componente invalido")

    @property
    def output_types(self):
        return {output.name: output.type for output in self.outputs}

    def resolve_params(self, params=None):
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise ValueError("params deve ser um mapa")
        known = {p.name for p in self.parameters}
        unknown = set(params) - known
        if unknown:
            raise ValueError(f"Parametro desconhecido: {', '.join(sorted(map(str, unknown)))}")
        return {p.name: p.validate(params.get(p.name, p.default)) for p in self.parameters}

    def calculate(self, candles, params=None):
        return tuple(self.calculator(candles, **self.resolve_params(params)))

    @property
    def key(self):
        return f"{self.id}@{self.version}"

    def describe(self, *, legacy=False):
        params_yaml = "\n".join(f"  {p.name}: {p.default}" for p in self.parameters)
        example = f'type: {self.id}\nversion: "{self.version}"\n'
        if not legacy:
            example = f"key: {self.key}\n"
        example += f"params:\n{params_yaml}\n" if params_yaml else "params: {}\n"
        return {
            **({} if legacy else {"key": self.key}),
            "id": self.id, "category": self.category, "version": self.version,
            "description": self.description, "inputs": list(self.inputs),
            "outputs": {o.name: o.describe() for o in self.outputs},
            "parameters": {p.name: p.describe() for p in self.parameters},
            "warmup": self.warmup, "initialization": self.initialization,
            "timing": self.timing, "example_yaml": example,
        }


_REGISTRY: dict[str, ComponentDefinition] = {}


def register(definition: ComponentDefinition):
    if not isinstance(definition, ComponentDefinition):
        raise ValueError("Registro exige ComponentDefinition")
    key = definition.key
    if key in _REGISTRY:
        raise ValueError(f"Componente duplicado: {definition.id}@{definition.version}")
    _REGISTRY[key] = definition
    return definition


def component_keys() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def get(identifier, version=None):
    if not isinstance(identifier, str):
        raise ValueError("ID de componente invalido")
    if "@" in identifier:
        if version is not None or identifier.count("@") != 1:
            raise ValueError("Chave de componente inválida")
        key = identifier
    elif version is not None:
        key = f"{identifier}@{version}" if isinstance(version, str) else ""
    else:
        # Unversioned IDs are supported only for older input and CLI lookup.
        available = [d for d in _REGISTRY.values() if d.id == identifier]
        if available:
            return max(available, key=lambda d: tuple(map(int, d.version.split("."))))
        key = f"{identifier}@default"
    if key in _REGISTRY:
        return _REGISTRY[key]
    raise ValueError(f"Componente/versao desconhecido: {key}")


def describe(identifier=None):
    if identifier is not None:
        return get(identifier).describe()
    return [definition.describe() for definition in sorted(_REGISTRY.values(), key=lambda d: (d.id, d.version))]


def catalog_snapshot(version: int):
    """Reconstruct the published wire shape; never modify the sealed bundle."""
    if type(version) is not int or version not in (1, 2):
        raise ValueError(f"Versão desconhecida do snapshot do catálogo: {version}")
    return [definition.describe(legacy=(version == 1))
            for definition in sorted(_REGISTRY.values(), key=lambda d: (d.id, d.version))]


CATALOG_SNAPSHOT_VERSION = 2


def component_definition(spec):
    if not isinstance(spec, Mapping):
        raise ValueError("Componente deve ser um mapa")
    if "key" in spec:
        if set(spec) - {"key", "params", "adapter_key", "adapter_provenance"}:
            raise ValueError("Chave de componente não aceita campos extras ou alias legado")
        key = spec["key"]
        if not isinstance(key, str) or "@" not in key:
            raise ValueError("Componente exige key versionada")
        return get(key)
    if set(spec) - {"type", "version", "params"}:
        raise ValueError("Campo de componente desconhecido")
    if "type" not in spec:
        raise ValueError("Componente exige key ou alias legado type")
    if "version" in spec and not isinstance(spec["version"], str):
        raise ValueError("Versao de componente deve ser texto")
    return get(spec["type"], spec.get("version"))


def _external_adapter(spec, definition):
    from .adapters.registry import default_registry

    resolution = default_registry().resolve(spec["adapter_key"])
    if not resolution.available:
        raise ValueError(f"adapter_key: {resolution.reason}")
    adapter = resolution.adapter
    if not all(callable(getattr(adapter, method, None)) for method in (
        "describe_component", "validate_params", "evaluate_with_provenance",
    )):
        raise ValueError("adapter_key: adapter não é um primitive")
    contract = adapter.describe_component(definition.key)
    library = resolution.library
    description = adapter.describe()
    provenance = {
        "library_id": library.id, "adapter_key": description.adapter_key,
        "adapter_version": description.adapter_version, "resolved_version": resolution.resolved_version,
        "license": library.license, "source": library.provenance["source"],
        "source_sha256": library.provenance["sha256"],
    }
    if "adapter_provenance" in spec and spec["adapter_provenance"] != provenance:
        raise ValueError("adapter_provenance não corresponde ao adapter registrado")
    return adapter, contract, provenance


def resolve_component(spec):
    definition = component_definition(spec)
    if "params" in spec and not isinstance(spec["params"], Mapping):
        raise ValueError("params deve ser um mapa")
    if "adapter_key" in spec:
        adapter, contract, provenance = _external_adapter(spec, definition)
        return ({"key": definition.key, "adapter_key": provenance["adapter_key"],
                 "params": dict(adapter.validate_params(definition.key, spec.get("params", {}))),
                 "adapter_provenance": provenance}, contract["outputs"], adapter)
    if "adapter_provenance" in spec:
        raise ValueError("adapter_provenance exige adapter_key")
    params = definition.resolve_params(spec.get("params"))
    if definition.id == "indicators.macd" and params["fast_period"] >= params["slow_period"]:
        raise ValueError("fast_period must be smaller than slow_period")
    return {"key": definition.key, "params": params}, definition.output_types, None


def validate_component(spec):
    return resolve_component(spec)[0]


def evaluate_component(spec, candles, *, on_invocation=None):
    from datetime import timedelta

    resolved, _, adapter = resolve_component(spec)
    definition = get(resolved["key"])
    if adapter is None:
        return definition.calculate(candles, resolved["params"])
    rows = tuple(candles)
    if not rows:
        return ()
    result = adapter.evaluate_with_provenance(
        definition.key, rows, resolved["params"], as_of=rows[-1].timestamp + timedelta(minutes=1))
    if on_invocation is not None:
        invocation = result.provenance.to_dict()
        lock = resolved["adapter_provenance"]
        for field in ("library_id", "adapter_key", "adapter_version", "resolved_version", "license"):
            if invocation[field] != lock[field]:
                raise ValueError(f"adapter provenance diverge da entrada registrada: {field}")
        if invocation["source"] != lock["source"]:
            invocation["implementation_source"] = invocation["source"]
        invocation.update(source=lock["source"], source_sha256=lock["source_sha256"])
        on_invocation(invocation)
    return result.observations


_TIMING = (
    "M1 candle_at identifies the opening T. Complete OHLCV and its observation are "
    "available at T+60 seconds. Gaps reset warmup and pending structures; duplicate "
    "or decreasing timestamps are rejected. No setup is retroactively confirmed."
)
_BIAS = (OutputSpec("bias", "bias", "Confirmed directional observation or no occurrence.",
                    ("bullish", "bearish", "none")),)
_OHLC = ("timestamp", "open", "high", "low", "close")


def _integer(name, default, description, minimum=1, maximum=10000):
    return ParameterSpec(name, "int", default, description, minimum, maximum)


def _pattern(identifier, description, calculator, parameters, warmup, initialization):
    register(ComponentDefinition(
        identifier, identifier.split(".")[0], "1.0.0", description, _OHLC,
        _BIAS, parameters, warmup, initialization, _TIMING, calculator,
    ))


register(ComponentDefinition(
    "indicators.ema", "indicators", "1.0.0", "Exponential moving average of closes.",
    ("timestamp", "close"), (OutputSpec("value", "number", "Exponential moving average."),),
    (_integer("period", 20, "Contiguous closed candles required for availability."),),
    "period contiguous candles, including the current candle.",
    "First close seeds EMA; alpha=2/(period+1), matching the original research calculation.",
    _TIMING, ema,
))
register(ComponentDefinition(
    "indicators.sma", "indicators", "1.0.0", "TA-Lib simple moving average of closes.",
    ("timestamp", "close"), (OutputSpec("value", "number", "Arithmetic mean of closing prices."),),
    (_integer("period", 20, "Contiguous closed candles in the mean.", 2),),
    "period contiguous candles, including the current candle.",
    "Arithmetic mean of the first period closes; recomputed after each M1 gap.",
    _TIMING, sma,
))
register(ComponentDefinition(
    "indicators.rsi", "indicators", "1.0.0", "TA-Lib Wilder relative strength index.",
    ("timestamp", "close"), (OutputSpec("value", "number", "Relative strength index, percent."),),
    (_integer("period", 14, "Number of close-to-close changes.", 2),),
    "period + 1 contiguous candles.",
    "Mean gain and loss of the first period changes seed Wilder smoothing.",
    _TIMING, rsi,
))
register(ComponentDefinition(
    "indicators.atr", "indicators", "1.0.0", "TA-Lib Wilder average true range.",
    ("timestamp", "high", "low", "close"),
    (OutputSpec("value", "number", "Wilder average true range."),),
    (_integer("period", 14, "Number of true ranges.", 2),),
    "period + 1 contiguous candles.",
    "Mean of first period true ranges seeds Wilder smoothing.",
    _TIMING, atr,
))
register(ComponentDefinition(
    "indicators.bollinger", "indicators", "1.0.0", "TA-Lib Bollinger bands using SMA.",
    ("timestamp", "close"),
    tuple(OutputSpec(name, "number", description) for name, description in (
        ("upper", "Upper band."), ("middle", "Simple moving average."),
        ("lower", "Lower band."))),
    (_integer("period", 20, "Contiguous closed candles in the window.", 2),
     ParameterSpec("deviation", "number", 2.0, "Population standard deviation multiplier.", 0, 100)),
    "period contiguous candles.",
    "SMA plus/minus deviation times population standard deviation.",
    _TIMING, bollinger,
))
register(ComponentDefinition(
    "indicators.macd", "indicators", "1.0.0", "TA-Lib MACD with EMA signal.",
    ("timestamp", "close"),
    tuple(OutputSpec(n, "number", n) for n in ("macd", "signal", "histogram")),
    (_integer("fast_period", 12, "Fast EMA period.", 2),
     _integer("slow_period", 26, "Slow EMA period.", 2),
     _integer("signal_period", 9, "Signal EMA period.", 2)),
    "slow_period + signal_period - 1 contiguous candles when fast_period < slow_period.",
    "TA-Lib EMA initialization and signal smoothing; fast period must be smaller than slow.",
    _TIMING, macd,
))
register(ComponentDefinition(
    "indicators.stochastic", "indicators", "1.0.0", "TA-Lib slow stochastic oscillator.",
    ("timestamp", "high", "low", "close"),
    (OutputSpec("k", "number", "Slow K, percent."), OutputSpec("d", "number", "Slow D, percent.")),
    (_integer("fast_k", 5, "Fast K lookback.", 1),
     _integer("slow_k", 3, "Slow K SMA period.", 1),
     _integer("slow_d", 3, "Slow D SMA period.", 1)),
    "fast_k + slow_k + slow_d - 2 contiguous candles.",
    "TA-Lib fast K over high/low window, followed by SMA for K and D.",
    _TIMING, stochastic,
))
register(ComponentDefinition(
    "indicators.supertrend", "indicators", "1.0.0", "Supertrend with Wilder ATR.",
    ("timestamp", "high", "low", "close"),
    (OutputSpec("value", "number", "Active trailing band."),
     OutputSpec("direction", "bias", "Current trend direction.", ("bullish", "bearish", "none"))),
    (_integer("period", 10, "Wilder ATR period.", 2),
     ParameterSpec("multiplier", "number", 3.0, "ATR band multiplier.", 0, 100)),
    "period + 1 contiguous candles.",
    "Seed bullish on first ATR; basic bands are (high+low)/2 +/- multiplier*ATR. "
    "Upper ratchets down unless previous close exceeds it; lower ratchets up unless previous "
    "close falls below it. Close below lower switches bearish; above upper switches bullish.",
    _TIMING, supertrend,
))
register(ComponentDefinition(
    "indicators.obv", "indicators", "1.0.0", "TA-Lib on-balance volume.",
    ("timestamp", "close", "volume"), (OutputSpec("value", "number", "Cumulative signed volume."),),
    (), "One candle with explicitly supplied finite non-negative volume.",
    "Seed with first candle volume, add or subtract later volume on rising or falling closes.",
    _TIMING, obv,
))
for _name, _function in (
    ("doji", "CDLDOJI"), ("hammer", "CDLHAMMER"),
    ("shooting_star", "CDLSHOOTINGSTAR"), ("inverted_hammer", "CDLINVERTEDHAMMER"),
):
    register(ComponentDefinition(
        "candles." + _name, "candles", "1.0.0",
        f"TA-Lib {_function} candlestick recognition.", _OHLC,
        (OutputSpec("detected", "bool", "Whether the pattern was recognized."),
         OutputSpec("score", "int", "Signed TA-Lib pattern result.")), (),
        "TA-Lib pattern lookback plus one contiguous candle.",
        "TA-Lib default candle settings; context and thresholds follow the library.",
        _TIMING, partial(candle_pattern, name=_function),
    ))
register(ComponentDefinition(
    "indicators.dmi", "indicators", "1.0.0", "Wilder directional movement and ADX.",
    ("timestamp", "high", "low", "close"),
    tuple(OutputSpec(name, "number", description) for name, description in (
        ("plus", "Positive directional index, percent."),
        ("minus", "Negative directional index, percent."),
        ("adx", "Wilder average directional index, percent."),
    )),
    (_integer("dmi_period", 14, "Wilder TR/DM smoothing period.", 2, 100),
     _integer("adx_smoothing", 14, "Wilder DX smoothing period.", 2, 100)),
    "dmi_period + adx_smoothing contiguous candles.",
    "First TR/DM uses candle two; arithmetic average seeds TR/DM and DX, then Wilder smoothing.",
    _TIMING, dmi,
))
_pattern("candles.engulfing", "Opposite real body fully engulfs the prior body with extension.",
         engulfing, (), "2 contiguous candles.", "No pending state; strict body extension.")
_pattern("candles.pin_reversal", "Directional rejection wick relative to the real body.",
         pin_reversal,
         (ParameterSpec("wick_body_ratio", "number", 2.0, "Minimum rejection wick/body.", 0, 100),
          ParameterSpec("opposite_wick_body_ratio", "number", 1.0,
                        "Maximum opposite wick/body.", 0, 100)),
         "1 closed candle.", "Nonzero body; wick ratios are recognition parameters.")
_pattern("price_action.inside_breakout", "Close breaks the outer bar of a strict inside bar.",
         inside_breakout, (), "3 contiguous candles.", "Two prior bars establish the structure.")
_pattern("price_action.hikkake", "False inside-bar escape followed by a closing reversal.",
         hikkake, (_integer("confirmation_bars", 3, "Bars after escape allowed to confirm."),),
         "6 contiguous candles; confirmation requires a later candle.",
         "Newest escape replaces pending setup; one confirmation per setup.")
_pattern("market.failed_breakout", "One-sided sweep of a prior range closes within it.",
         failed_breakout, (_integer("lookback", 20, "Prior range window.", 2),),
         "lookback + 1 contiguous candles.", "Both-boundary sweeps are ambiguous, bias none.")
_pattern("market.breakout_retest", "Closing breakout followed by a retest that holds its level.",
         breakout_retest,
         (_integer("lookback", 10, "Prior range window.", 2),
          _integer("confirmation_bars", 3, "Bars after breakout allowed to confirm.")),
         "lookback + 2 contiguous candles, including a later confirmation candle.",
         "Confirm existing setup first; failure to hold invalidates; one confirmation per setup.")
