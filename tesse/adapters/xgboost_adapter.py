"""Pinned, CPU-only binary learner for offline research; import XGBoost on demand."""

from collections.abc import Mapping
from math import isfinite

import numpy as np

from .contracts import AdapterDescription, AdapterHealth, AdapterInvocation, AdapterProvenance
from .sklearn_adapter import hash_array, hash_parameters

XGB_KEY = 'learners.xgboost.gradient_boosting@1.0.0'
SOURCE = 'https://pypi.org/project/xgboost/3.4.1/'
# These bounds also define the Forge node's declarative I/O contract.
PARAMETERS = {
    'n_estimators': (100, 1, 10_000),
    'learning_rate': (0.05, 1e-9, 1.0),
    'max_depth': (3, 1, 16),
    'min_child_weight': (5.0, 0.0, 1_000_000.0),
    'reg_lambda': (10.0, 0.0, 1_000_000.0),
    'random_state': (0, 0, 2**31 - 1),
}
FIXED = {
    'objective': 'binary:logistic', 'tree_method': 'hist', 'device': 'cpu', 'n_jobs': 1,
    'subsample': 1.0, 'colsample_bytree': 1.0, 'colsample_bylevel': 1.0,
    'colsample_bynode': 1.0, 'early_stopping_rounds': None, 'callbacks': None,
}


def _xgboost():
    import xgboost

    return xgboost


def _parameters(params):
    if not isinstance(params, Mapping) or params.keys() - PARAMETERS.keys():
        raise ValueError('xgboost: parâmetros fora do contrato')
    result = dict(FIXED)
    for name, (default, low, high) in PARAMETERS.items():
        value = params.get(name, default)
        accepted = (int, np.integer) if type(default) is int else (int, float, np.integer, np.floating)
        if (isinstance(value, (bool, np.bool_)) or not isinstance(value, accepted)
                or not low <= value <= high or not isfinite(value)):
            raise ValueError(f'{name}: esperado {type(default).__name__} entre {low} e {high}')
        result[name] = type(default)(value)
    return result


def _features(value):
    arr = np.asarray(value)
    if arr.dtype.kind not in 'fiu' or arr.ndim != 2 or not all(arr.shape):
        raise ValueError('features: esperada matriz numérica 2-D não vazia')
    arr = arr.astype(np.float32, copy=False)
    if not np.isfinite(arr).all():
        raise ValueError('features: valores devem ser finitos em float32')
    return arr


def _labels(value, count):
    arr = np.asarray(value)
    if (arr.dtype.kind not in 'biuf' or arr.shape != (count,)
            or not np.array_equal(np.unique(arr), [0, 1])):
        raise ValueError('labels: esperadas exatamente as classes 0 e 1, uma por linha')
    return arr.astype(np.int8, copy=False)


class XGBoostAdapter:
    def __init__(self):
        self._invocations = []

    @property
    def invocations(self):
        return tuple(self._invocations)

    def describe(self):
        return AdapterDescription('xgboost', XGB_KEY, ('classification', 'probability', 'native_json'))

    def health_check(self):
        try:
            library = _xgboost()
            library.XGBClassifier(**FIXED)
            return AdapterHealth(True, str(library.__version__))
        except Exception as exc:
            return AdapterHealth(False, reason=f'xgboost indisponível: {type(exc).__name__}; instale ml-xgboost')

    def fit(self, features, labels, params=None):
        X = _features(features)
        y = _labels(labels, len(X))
        effective = _parameters({} if params is None else params)
        library = _xgboost()
        model = library.XGBClassifier(**effective).fit(X, y)
        inputs = {'features': hash_array(X), 'labels': hash_array(y)}
        provenance = AdapterProvenance(self.describe(), str(library.__version__), 'Apache-2.0',
                                       SOURCE, effective, {'parameters': hash_parameters(effective), **inputs})
        self._invocations.append(AdapterInvocation('fit', provenance, inputs, {
            'training_probabilities': hash_array(self.predict_proba(model, X)),
        }))
        return model

    @staticmethod
    def _check_model(model):
        if (not isinstance(model, _xgboost().XGBClassifier) or not model.__sklearn_is_fitted__()
                or not np.array_equal(model.classes_, [0, 1])):
            raise ValueError('model: esperado XGBClassifier binário ajustado')

    def predict_proba(self, model, features):
        X = _features(features)
        self._check_model(model)
        if X.shape[1] != model.n_features_in_:
            raise ValueError('features: número de colunas diverge do modelo')
        prob = np.asarray(model.predict_proba(X), dtype=float)
        if (prob.shape != (len(X), 2) or not np.isfinite(prob).all() or np.any((prob < 0) | (prob > 1))
                or not np.allclose(prob.sum(axis=1), 1, rtol=0, atol=1e-7)):
            raise ValueError('predict_proba: probabilidades binárias inválidas')
        return prob

    def model_json(self, model):
        self._check_model(model)
        return bytes(model.get_booster().save_raw(raw_format='json'))
