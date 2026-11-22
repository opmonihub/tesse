"""Explicit TA-Lib oracle, independent of the runtime registry and trading worker.

EMA uses TA-Lib's arithmetic-mean seed, unlike the native first-close EMA.
DMI uses TA-Lib's DI seed and ADX lookback (2*n candles), unlike the native
arithmetic TR/DM seed (n + smoothing candles). Independent ADX smoothing is
unsupported. Engulfing exposes TA-Lib detected/score, not native bias: TA-Lib
requires three candles and distinguishes scores 80/100 at equal body edges.
All gaps reset lookback; warmup NaNs become unavailable observations.
"""

import hashlib
import importlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

import numpy as np

from tesse.core import Candle
from tesse.indicators.base import MINUTE, Observation, continuity, observation, valid_volume

from .contracts import AdapterDescription, AdapterHealth, AdapterProvenance, Parameters
from .registry import Registration

# This is deliberately an allowlist, not getattr based on declarative input.
_PATTERNS = {
    'candles.doji': 'CDLDOJI', 'candles.hammer': 'CDLHAMMER',
    'candles.shooting_star': 'CDLSHOOTINGSTAR', 'candles.inverted_hammer': 'CDLINVERTEDHAMMER',
    'candles.engulfing': 'CDLENGULFING',
}
_INDICATORS = {
    'indicators.ema': ('EMA', ('close',), ('value',), {'period': 20}),
    'indicators.sma': ('SMA', ('close',), ('value',), {'period': 20}),
    'indicators.rsi': ('RSI', ('close',), ('value',), {'period': 14}),
    'indicators.atr': ('ATR', ('high', 'low', 'close'), ('value',), {'period': 14}),
    'indicators.dmi': ('ADX', ('high', 'low', 'close'), ('plus', 'minus', 'adx'),
                       {'dmi_period': 14, 'adx_smoothing': 14}),
    'indicators.bollinger': ('BBANDS', ('close',), ('upper', 'middle', 'lower'),
                             {'period': 20, 'deviation': 2.0}),
    'indicators.macd': ('MACD', ('close',), ('macd', 'signal', 'histogram'),
                       {'fast_period': 12, 'slow_period': 26, 'signal_period': 9}),
    'indicators.stochastic': ('STOCH', ('high', 'low', 'close'), ('k', 'd'),
                             {'fast_k': 5, 'slow_k': 3, 'slow_d': 3}),
    'indicators.obv': ('OBV', ('close', 'volume'), ('value',), {}),
}


def _load_talib():
    return importlib.import_module('talib')


def _component(component):
    if not isinstance(component, str):
        raise ValueError('component must be an allowlisted key')
    identifier, separator, version = component.partition('@')
    if separator and version != '1.0.0':
        raise ValueError('unsupported component version')
    if identifier not in _INDICATORS and identifier not in _PATTERNS:
        raise ValueError('component is not in the TA-Lib allowlist')
    return identifier


def validate_params(component: str, params: Parameters) -> dict:
    """Validate declarative configuration without loading TA-Lib or reading candles."""
    component = _component(component)
    defaults = _INDICATORS[component][3] if component in _INDICATORS else {}
    if not isinstance(params, Mapping) or set(params) - set(defaults):
        raise ValueError('unknown or invalid parameters')
    resolved = {**defaults, **params}
    for name, value in resolved.items():
        if name == 'deviation':
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError('deviation must be finite between 0 and 100')
        else:
            minimum = 1 if component == 'indicators.stochastic' else 2
            maximum = 100 if component == 'indicators.dmi' else 10000
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')
    if component == 'indicators.dmi' and resolved['dmi_period'] != resolved['adx_smoothing']:
        raise ValueError('TA-Lib DMI requires equal dmi_period and adx_smoothing')
    if component == 'indicators.macd' and resolved['fast_period'] >= resolved['slow_period']:
        raise ValueError('fast_period must be smaller than slow_period')
    return resolved


@dataclass(frozen=True)
class PrimitiveInvocation:
    observations: tuple[Observation, ...]
    provenance: AdapterProvenance


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class TALibAdapter:
    library_id = 'talib'
    adapter_key = 'library.talib@1.0.0'
    adapter_version = '1.0.0'
    capabilities = ('primitive', 'indicators', 'candlestick_patterns')

    def describe(self) -> AdapterDescription:
        return AdapterDescription(self.library_id, self.adapter_key, self.capabilities)

    def health_check(self) -> AdapterHealth:
        try:
            library = _load_talib()
            # Mutable process settings affect numerical reproducibility; never mutate them here.
            if library.get_compatibility() != 0 or any(
                library.get_unstable_period(name) for name in ('EMA', 'RSI', 'ATR', 'ADX', 'PLUS_DI', 'MINUS_DI')
            ):
                return AdapterHealth(False, reason='TA-Lib requires default compatibility and unstable periods')
            return AdapterHealth(True, library.__version__)
        except (ImportError, OSError, AttributeError) as exc:
            return AdapterHealth(False, reason=f'TA-Lib unavailable: {type(exc).__name__}')

    validate_params = staticmethod(validate_params)

    def describe_component(self, component: str) -> dict:
        component = _component(component)
        fields = (_INDICATORS[component][1] if component in _INDICATORS else
                  ('open', 'high', 'low', 'close'))
        outputs = ({name: 'number' for name in _INDICATORS[component][2]} if component in _INDICATORS
                   else {'detected': 'bool', 'score': 'int'})
        initialization = {
            'indicators.ema': 'TA-Lib arithmetic mean seed; differs from native first-close EMA seed.',
            'indicators.dmi': 'TA-Lib DI seed and ADX; differs from native arithmetic TR/DM seed. Equal periods required.',
            'candles.engulfing': 'TA-Lib scores 80/100 with three-candle lookback; differs from native two-candle bias.',
        }.get(component, 'TA-Lib default settings; equivalent to the existing TA-Lib native wrapper.')
        warmup = {
            'indicators.ema': 'period candles', 'indicators.sma': 'period candles',
            'indicators.rsi': 'period + 1 candles', 'indicators.atr': 'period + 1 candles',
            'indicators.dmi': '2 * dmi_period candles', 'indicators.obv': 'one candle with explicit volume',
            'indicators.bollinger': 'period candles',
            'indicators.macd': 'slow_period + signal_period - 1 candles',
            'indicators.stochastic': 'fast_k + slow_k + slow_d - 2 candles',
        }.get(component, 'TA-Lib abstract.Function lookback + 1 candles')
        return {'key': component + '@1.0.0', 'inputs': ('timestamp', *fields), 'outputs': outputs,
                'parameters': validate_params(component, {}), 'warmup': warmup,
                'initialization': initialization,
                'timing': 'Closed M1 only: candle_at + 1 minute <= as_of. Reset after every gap; no signal shift.'}

    def evaluate(self, component: str, candles: Sequence[Candle], params: Parameters,
                 *, as_of: datetime) -> tuple[Observation, ...]:
        return self.evaluate_with_provenance(component, candles, params, as_of=as_of).observations

    def evaluate_with_provenance(self, component: str, candles: Sequence[Candle], params: Parameters,
                                 *, as_of: datetime) -> PrimitiveInvocation:
        component = _component(component)
        parameters = validate_params(component, params)
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
            raise ValueError('as_of must be a UTC datetime')
        health = self.health_check()
        if not health.available:
            raise RuntimeError(f'TA-Lib unavailable: {health.reason}')
        library = _load_talib()
        # Future candles do not enter calculation or invocation evidence.
        rows = tuple(row for row in candles if row.timestamp + MINUTE <= as_of)
        observations = []
        segment = []
        for row, count in continuity(rows):
            if count == 1 and segment:
                observations.extend(self._calculate(library, component, segment, parameters))
                segment = []
            segment.append(row)
        if segment:
            observations.extend(self._calculate(library, component, segment, parameters))
        fields = self.describe_component(component)['inputs']
        inputs = [[getattr(row, field).isoformat() if field == 'timestamp' else getattr(row, field)
                   for field in fields] for row in rows]
        outputs = [{'candle_at': row.candle_at.isoformat(), 'available_at': row.available_at.isoformat(),
                    'available': row.available, 'reason': row.reason, 'values': dict(row.values)}
                   for row in observations]
        hashes = {'inputs': _hash(inputs), 'outputs': _hash(outputs)}
        hashes['invocation'] = _hash({'component': component + '@1.0.0', 'params': parameters,
                                      'as_of': as_of.isoformat(), 'hashes': hashes,
                                      'adapter_key': self.adapter_key, 'version': health.resolved_version,
                                      'ta_version': library.__ta_version__.decode('ascii')})
        provenance = AdapterProvenance(self.describe(), health.resolved_version, 'BSD-2-Clause',
                                      'https://github.com/TA-Lib/ta-lib-python/tree/v' + health.resolved_version,
                                      MappingProxyType(parameters), MappingProxyType(hashes))
        return PrimitiveInvocation(tuple(observations), provenance)

    @staticmethod
    def _calculate(library, component, rows, params):
        if component in _PATTERNS:
            from talib import abstract
            function_name = _PATTERNS[component]
            lookback = abstract.Function(function_name).lookback
            arrays = [np.array([getattr(row, field) for row in rows], dtype=np.float64)
                      for field in ('open', 'high', 'low', 'close')]
            scores = getattr(library, function_name)(*arrays)
            return [observation(row, {'detected': bool(score), 'score': int(score)} if index >= lookback else {},
                                available=index >= lookback, reason=None if index >= lookback else 'warmup')
                    for index, (row, score) in enumerate(zip(rows, scores, strict=True))]
        function, fields, outputs, _ = _INDICATORS[component]
        if 'volume' in fields and any(not valid_volume(row.volume) for row in rows):
            raise ValueError('OBV requires explicit finite nonnegative volume')
        arrays = [np.array([getattr(row, field) for row in rows], dtype=np.float64) for field in fields]
        if component == 'indicators.dmi':
            values = tuple(getattr(library, name)(*arrays, timeperiod=params['dmi_period'])
                           for name in ('PLUS_DI', 'MINUS_DI', 'ADX'))
        else:
            kwargs = {}
            if 'period' in params:
                kwargs['timeperiod'] = params['period']
            if component == 'indicators.bollinger':
                kwargs.update(nbdevup=params['deviation'], nbdevdn=params['deviation'], matype=0)
            if component == 'indicators.macd':
                kwargs.update(fastperiod=params['fast_period'], slowperiod=params['slow_period'], signalperiod=params['signal_period'])
            if component == 'indicators.stochastic':
                kwargs.update(fastk_period=params['fast_k'], slowk_period=params['slow_k'], slowd_period=params['slow_d'], slowk_matype=0, slowd_matype=0)
            result = getattr(library, function)(*arrays, **kwargs)
            values = (result,) if len(outputs) == 1 else result
        results = []
        for row, *items in zip(rows, *values, strict=True):
            available = all(math.isfinite(item) for item in items)
            results.append(observation(row, dict(zip(outputs, map(float, items), strict=True)) if available else {},
                                       available=available, reason=None if available else 'warmup'))
        return results


REGISTRATION = Registration(TALibAdapter.adapter_key, TALibAdapter.library_id, TALibAdapter)
