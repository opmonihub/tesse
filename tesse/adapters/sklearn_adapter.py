"""Governed, deterministic scikit-learn adapters.

The module deliberately contains no worker or router integration.  Adapters are
resolved by their stable keys and expose plain numpy/sklearn values so callers
can persist parameters and hashes as provenance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .contracts import AdapterDescription, AdapterHealth, AdapterInvocation, AdapterProvenance
from .registry import Registration

HGB_KEY = "learners.sklearn.hist_gradient_boosting@1.0.0"
SPLIT_KEY = "selection.sklearn.time_series_split@1.0.0"
SIGMOID_KEY = "calibration.sklearn.sigmoid@1.0.0"


def _sklearn() -> Any:
    try:
        import sklearn  # type: ignore
        return sklearn
    except Exception as exc:  # pragma: no cover - exercised in minimal installs
        raise RuntimeError("scikit-learn extra não instalado") from exc


def _matrix(value: Any, name: str) -> NDArray[np.float64]:
    arr = np.asarray(value, dtype=float)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError(f"{name}: esperada matriz 2-D não vazia")
    if not np.isfinite(arr).all():
        raise ValueError(f"{name}: valores devem ser finitos")
    return arr


def _labels(value: Any, n: int) -> NDArray[Any]:
    arr = np.asarray(value)
    if arr.ndim != 1 or len(arr) != n:
        raise ValueError("labels: dimensão incompatível")
    if len(np.unique(arr)) < 2:
        raise ValueError("labels: são necessárias pelo menos duas classes")
    if arr.dtype.kind in "f":
        if not np.isfinite(arr).all():
            raise ValueError("labels: valores devem ser finitos")
    elif arr.dtype.kind not in "biuSU":
        raise ValueError("labels: tipo não suportado")
    return arr


def _strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < minimum:
        raise ValueError(f"{name}: esperado inteiro >= {minimum}")
    return int(value)


def hash_array(value: Any) -> str:
    """Hash dtype, shape and bytes, making equal fixtures reproducible."""
    arr = np.asarray(value)
    if arr.dtype.kind == "O":
        if not all(isinstance(item, (str, int, float, bool, type(None))) for item in arr.flat):
            raise ValueError("hash: object arbitrário não suportado")
        payload = json.dumps({"dtype": str(arr.dtype), "shape": list(arr.shape), "values": arr.tolist()},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
        return hashlib.sha256(payload).hexdigest()
    arr = np.ascontiguousarray(arr)
    h = hashlib.sha256()
    h.update(str(arr.dtype).encode())
    h.update(json.dumps(list(arr.shape), separators=(",", ":")).encode())
    h.update(arr.tobytes(order="C"))
    return h.hexdigest()


def hash_parameters(parameters: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(parameters), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class SklearnHistGradientBoostingAdapter:
    library_id = "sklearn"
    adapter_key = HGB_KEY
    adapter_version = "1.0.0"

    def __init__(self) -> None:
        self._invocations: list[AdapterInvocation] = []

    @property
    def invocations(self) -> tuple[AdapterInvocation, ...]:
        return tuple(self._invocations)

    def describe(self) -> AdapterDescription:
        return AdapterDescription(self.library_id, self.adapter_key,
                                  ("classification", "probability", "deterministic"))

    def health_check(self) -> AdapterHealth:
        try:
            version = str(_sklearn().__version__)
            from sklearn.ensemble import HistGradientBoostingClassifier
            HistGradientBoostingClassifier(early_stopping=False)
            return AdapterHealth(True, version)
        except Exception as exc:
            return AdapterHealth(False, reason=f"sklearn indisponível: {type(exc).__name__}")

    def _make(self, params: Mapping[str, Any]) -> Any:
        from sklearn.ensemble import HistGradientBoostingClassifier
        allowed = {"learning_rate", "max_iter", "max_leaf_nodes", "max_depth",
                   "min_samples_leaf", "l2_regularization", "max_bins", "categorical_features",
                   "monotonic_cst"}
        unknown = set(params) - allowed - {"random_state"}
        if unknown:
            raise ValueError(f"parâmetros desconhecidos: {', '.join(sorted(unknown))}")
        seed = params.get("random_state", 0)
        if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or int(seed) < 0:
            raise ValueError("random_state: esperado inteiro não negativo")
        constraints = params.get("monotonic_cst")
        if constraints is not None and (
            not isinstance(constraints, (list, tuple)) or not constraints
            or any(type(c) is not int or c not in (-1, 0, 1) for c in constraints)
        ):
            raise ValueError("monotonic_cst: esperada lista de inteiros em {-1, 0, 1}")
        kwargs = {k: params[k] for k in allowed if k in params}
        # Explicitly prohibit stochastic early stopping and bind a seed for provenance.
        kwargs.update(early_stopping=False, random_state=int(params.get("random_state", 0)))
        return HistGradientBoostingClassifier(**kwargs)

    def fit(self, features: Any, labels: Any, params: Mapping[str, Any] | None = None) -> Any:
        X = _matrix(features, "features")
        y = _labels(labels, len(X))
        model = self._make(params or {})
        model.fit(X, y)
        prov = self.provenance(params or {}, X, y)
        self._invocations.append(AdapterInvocation("fit", prov,
                                            {"features": hash_array(X), "labels": hash_array(y)},
                                            {"training_probabilities": hash_array(model.predict_proba(X))}))
        return model

    def predict_proba(self, model: Any, features: Any) -> NDArray[np.float64]:
        X = _matrix(features, "features")
        from sklearn.ensemble import HistGradientBoostingClassifier

        if not isinstance(model, HistGradientBoostingClassifier):
            raise ValueError("model: modelo incompatível")
        probabilities = np.asarray(model.predict_proba(X), dtype=float)
        if probabilities.ndim != 2 or len(probabilities) != len(X) or not np.isfinite(probabilities).all():
            raise ValueError("predict_proba: probabilidades inválidas")
        if np.any(probabilities < 0) or not np.allclose(probabilities.sum(axis=1), 1.0, atol=1e-8):
            raise ValueError("predict_proba: probabilidades não normalizadas")
        effective = model.get_params()
        self._invocations.append(AdapterInvocation("predict_proba", self._provenance(effective),
                                                   {"features": hash_array(X)},
                                                   {"probabilities": hash_array(probabilities)}))
        return probabilities

    def provenance(self, params: Mapping[str, Any], features: Any | None = None,
                   labels: Any | None = None) -> AdapterProvenance:
        return self._provenance(self._make(params).get_params(), features, labels)

    def _provenance(self, effective, features=None, labels=None) -> AdapterProvenance:
        health = self.health_check()
        if not health.available:
            raise RuntimeError(health.reason)
        hashes = {"parameters": hash_parameters(effective)}
        if features is not None:
            hashes["features"] = hash_array(features)
        if labels is not None:
            hashes["labels"] = hash_array(labels)
        return AdapterProvenance(self.describe(), health.resolved_version or "", "BSD-3-Clause",
                                 "https://scikit-learn.org/stable/modules/ensemble.html",
                                 effective, hashes)


class SklearnTimeSeriesSplitAdapter:
    library_id = "sklearn"
    adapter_key = SPLIT_KEY

    def __init__(self) -> None:
        self._invocations: list[AdapterInvocation] = []

    @property
    def invocations(self) -> tuple[AdapterInvocation, ...]:
        return tuple(self._invocations)

    def describe(self) -> AdapterDescription:
        return AdapterDescription(self.library_id, self.adapter_key, ("temporal_split", "gap"))

    def health_check(self) -> AdapterHealth:
        try:
            return AdapterHealth(True, str(_sklearn().__version__))
        except Exception as exc:
            return AdapterHealth(False, reason=f"sklearn indisponível: {type(exc).__name__}")

    @staticmethod
    def _parameters(params):
        p = dict(params or {})
        unknown = set(p) - {"gap", "n_splits", "test_size", "max_train_size"}
        if unknown:
            raise ValueError(f"parâmetros desconhecidos: {', '.join(sorted(unknown))}")
        effective = {"gap": _strict_int(p.get("gap", 0), "gap"),
                     "n_splits": _strict_int(p.get("n_splits", 5), "n_splits", minimum=2)}
        for key in ("test_size", "max_train_size"):
            effective[key] = _strict_int(p[key], key, minimum=1) if key in p else None
        return effective

    def split(self, features: Any, labels: Any, params: Mapping[str, Any] | None = None):
        from sklearn.model_selection import TimeSeriesSplit

        X = _matrix(features, "features")
        y = _labels(labels, len(X))
        provenance = self.provenance(params)
        folds = tuple(TimeSeriesSplit(**provenance.parameters).split(X))
        fold_hashes = [{'train': hash_array(train), 'test': hash_array(test)} for train, test in folds]
        self._invocations.append(AdapterInvocation(
            "split", provenance, {"features": hash_array(X), "labels": hash_array(y)},
            {"folds": hash_parameters({"folds": fold_hashes})}))
        return iter(folds)

    def provenance(self, params: Mapping[str, Any] | None = None) -> AdapterProvenance:
        effective = self._parameters(params)
        health = self.health_check()
        if not health.available:
            raise RuntimeError(health.reason)
        return AdapterProvenance(self.describe(), health.resolved_version or "", "BSD-3-Clause",
                                 "https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html",
                                 effective, {"parameters": hash_parameters(effective)})

    def fit_transformer(self, transformer: Any, features: Any, train_indices: Any,
                        test_indices: Any | None = None):
        """Fit on train only, then record hashes for the exact partitions used."""
        X = _matrix(features, "features")
        train = _indices(train_indices, len(X), "train_indices")
        test = _indices(test_indices, len(X), "test_indices") if test_indices is not None else None
        if test is not None and train.max() >= test.min():
            raise ValueError("train_indices devem preceder e não sobrepor test_indices")
        health = self.health_check()
        if not health.available:
            raise RuntimeError(health.reason)
        parameters = dict(transformer.get_params(), transformer=type(transformer).__qualname__)
        provenance = AdapterProvenance(self.describe(), health.resolved_version or "", "BSD-3-Clause",
                                       "https://scikit-learn.org/stable/modules/preprocessing.html",
                                       parameters, {"parameters": hash_parameters(parameters)})
        transformer.fit(X[train])
        train_out = transformer.transform(X[train])
        inputs = {"features": hash_array(X), "train_indices": hash_array(train)}
        outputs = {"train": hash_array(train_out)}
        if test is None:
            result = train_out
        else:
            test_out = transformer.transform(X[test])
            result = train_out, test_out
            inputs['test_indices'] = hash_array(test)
            outputs['test'] = hash_array(test_out)
        self._invocations.append(AdapterInvocation("fit_transformer", provenance, inputs, outputs))
        return result


@dataclass
class TemporalSigmoidCalibrator:
    base_model: Any
    calibrator: Any
    classes_: NDArray[Any]

    def predict_proba(self, features: Any) -> NDArray[np.float64]:
        X = _matrix(features, "features")
        raw = np.asarray(self.base_model.predict_proba(X), dtype=float)
        eps = np.finfo(float).eps
        logits = np.log(np.clip(raw[:, 1], eps, 1 - eps) /
                        np.clip(1 - raw[:, 1], eps, 1 - eps))
        out = self.calibrator.predict_proba(logits.reshape(-1, 1))
        if not np.isfinite(out).all():
            raise ValueError("calibração produziu probabilidades inválidas")
        return out


def _indices(value: Any, n: int, name: str) -> NDArray[np.int64]:
    arr = np.asarray(value, dtype=object)
    if arr.ndim != 1 or any(isinstance(i, (bool, np.bool_)) or not isinstance(i, (int, np.integer))
                            for i in arr):
        raise ValueError(f"{name}: índices inteiros 1-D esperados")
    if not len(arr) or any(not 0 <= i < n for i in arr) or len(set(arr)) != len(arr):
        raise ValueError(f"{name}: índice fora da faixa ou duplicado")
    return arr.astype(np.int64)


class SklearnSigmoidCalibrationAdapter:
    library_id = "sklearn"
    adapter_key = SIGMOID_KEY

    def __init__(self) -> None:
        self._invocations: list[AdapterInvocation] = []

    @property
    def invocations(self) -> tuple[AdapterInvocation, ...]:
        return tuple(self._invocations)

    def describe(self) -> AdapterDescription:
        return AdapterDescription(self.library_id, self.adapter_key, ("calibration", "sigmoid", "oos"))

    def health_check(self) -> AdapterHealth:
        try:
            return AdapterHealth(True, str(_sklearn().__version__))
        except Exception as exc:
            return AdapterHealth(False, reason=f"sklearn indisponível: {type(exc).__name__}")

    def calibrate(self, learner: SklearnHistGradientBoostingAdapter, features: Any, labels: Any,
                  params: Mapping[str, Any] | None = None, *, gap: int = 0) -> TemporalSigmoidCalibrator:
        from sklearn.linear_model import LogisticRegression
        X = _matrix(features, "features")
        y = _labels(labels, len(X))
        p = dict(params or {})
        unknown = set(p) - {"cv", "n_splits", "learner_params"}
        if unknown:
            raise ValueError(f"parâmetros desconhecidos: {', '.join(sorted(unknown))}")
        if p.get("cv") not in (None, "time_series"):
            raise ValueError("calibração rejeita CV aleatório; use time_series")
        n_splits = _strict_int(p.get("n_splits", 3), "n_splits", minimum=2)
        gap = _strict_int(gap, "gap")
        splitter = SklearnTimeSeriesSplitAdapter()
        oos_x, oos_y = [], []
        base_params = p.get("learner_params", {})
        if not isinstance(base_params, Mapping):
            raise ValueError("learner_params: esperado objeto")
        if len(np.unique(y)) != 2:
            raise ValueError("calibração sigmoid requer exatamente duas classes")
        for train, test in splitter.split(X, y, {"n_splits": n_splits, "gap": gap}):
            if len(np.unique(y[train])) < 2:
                raise ValueError("calibração OOS: fold de treino sem ambas as classes")
            model = learner.fit(X[train], y[train], base_params)
            oos_x.append(learner.predict_proba(model, X[test]))
            oos_y.append(y[test])
        probs = np.vstack(oos_x)
        targets = np.concatenate(oos_y)
        if len(np.unique(targets)) < 2:
            raise ValueError("calibração OOS requer duas classes")
        # Logistic regression over log-odds is a sigmoid calibrator and is fit only on OOS rows.
        eps = np.finfo(float).eps
        logits = np.log(np.clip(probs[:, 1], eps, 1 - eps) / np.clip(1 - probs[:, 1], eps, 1 - eps))
        calibrator = LogisticRegression(random_state=0, solver="lbfgs").fit(logits.reshape(-1, 1), targets)
        final = learner.fit(X, y, base_params)
        result = TemporalSigmoidCalibrator(final, calibrator, np.asarray(final.classes_))
        prov = self.provenance(p, gap=gap)
        self._invocations.append(AdapterInvocation("calibrate", prov,
                                            {"features": hash_array(X), "labels": hash_array(y)},
                                            {"oos_probabilities": hash_array(probs),
                                             "oos_labels": hash_array(targets),
                                             "sigmoid_coefficient": hash_array(calibrator.coef_),
                                             "sigmoid_intercept": hash_array(calibrator.intercept_)}))
        return result

    def provenance(self, params: Mapping[str, Any] | None = None, *, gap: int = 0) -> AdapterProvenance:
        health = self.health_check()
        if not health.available:
            raise RuntimeError(health.reason)
        effective = {"n_splits": 3, "gap": gap, "cv": "time_series", **dict(params or {})}
        return AdapterProvenance(self.describe(), health.resolved_version or "", "BSD-3-Clause",
                                 "https://scikit-learn.org/stable/modules/calibration.html",
                                 effective, {"parameters": hash_parameters(effective)})


def registrations() -> tuple[Registration, ...]:
    """Registrations are explicit; no YAML/module-path discovery is performed."""
    return (Registration(HGB_KEY, "sklearn", SklearnHistGradientBoostingAdapter),
            Registration(SPLIT_KEY, "sklearn", SklearnTimeSeriesSplitAdapter),
            Registration(SIGMOID_KEY, "sklearn", SklearnSigmoidCalibrationAdapter))


REGISTRATIONS = registrations()


HGBAdapter = SklearnHistGradientBoostingAdapter
TimeSeriesSplitAdapter = SklearnTimeSeriesSplitAdapter
SigmoidCalibrationAdapter = SklearnSigmoidCalibrationAdapter
