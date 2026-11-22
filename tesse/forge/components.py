"""Reusable research components for a two-arm M1 comparison."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import numpy as np

from ..core import Candle, Params, price_movement, signals
from ..primitives.features import orient_columns, trailing_population_std
from ..primitives.ranking import (
    EdgeRankCalibration,
    calibrate_edge_rank,
    edge_rank_masks,
    rank_cutoff,
)
from .contracts import Invalid

DECISION_SECONDS = 62
ENTRY_SECONDS = 120
EXPIRY_SECONDS = 180
LABEL_SECONDS = 182
VOLATILITY_PERIODS = (1, 5, 15)
VOLATILITY_SIDE_PERIOD = 5
VOLATILITY_LOOKBACK = 15


@dataclass(frozen=True)
class Features:
    anchors: np.ndarray
    periods: tuple[int, ...]
    values: np.ndarray


@dataclass(frozen=True)
class Frame:
    anchors: np.ndarray
    sides: np.ndarray
    raw: np.ndarray
    x: np.ndarray

    def where(self, mask):
        return Frame(self.anchors[mask], self.sides[mask], self.raw[mask], self.x[mask])


@dataclass(frozen=True)
class Labels:
    anchors: np.ndarray
    available_at: np.ndarray
    y: np.ndarray


@dataclass(frozen=True)
class Training:
    frame: Frame
    y: np.ndarray

    def where(self, mask):
        return Training(self.frame.where(mask), self.y[mask])


@dataclass(frozen=True)
class Fitted:
    adapter: Any
    model: Any
    receipt: dict


@dataclass(frozen=True)
class Scored:
    frame: Frame
    probabilities: np.ndarray


@dataclass(frozen=True)
class _SelectionTrace:
    frame: Frame
    proposed: np.ndarray
    reasons: np.ndarray
    probabilities: np.ndarray | None = None


@dataclass(frozen=True)
class _RankTrace:
    cutoffs: dict[str, float]
    calibration: _SelectionTrace
    evaluation: _SelectionTrace


@dataclass(frozen=True)
class Proposals:
    anchors: np.ndarray
    sides: np.ndarray
    rank_fit: float | EdgeRankCalibration | None = field(default=None, repr=False, compare=False)
    trace: _RankTrace | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PhaseTrace:
    """Observed decisions; outcomes on rows without a settlement are hypothetical."""

    cutoffs: dict[str, float]
    rows: tuple[dict, ...]


@dataclass(frozen=True)
class Book:
    trades: tuple[dict, ...]
    proposed: int
    blocked_overlap: int
    outside_window: int
    trace: Mapping[str, PhaseTrace] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Comparison:
    control: Book
    candidate: Book
    metrics: dict


def dmi_proposals(snapshot, window, *, dmi_period, adx_smoothing, min_adx):
    """Warm up on prior candles; emit crossovers only for in-window decisions."""
    params = Params(dmi_period, adx_smoothing, min_adx)
    start, end = int(window.start.timestamp()), int(window.end.timestamp())
    bars = snapshot.bars
    limit = np.searchsorted(bars["t"], end - DECISION_SECONDS)
    candles = (
        Candle(datetime.fromtimestamp(int(row["t"]), UTC),
               *(float(row[key]) for key in ("o", "h", "l", "c", "v")))
        for row in bars[:limit]
    )
    anchors, sides = [], []
    for signal in signals(snapshot.asset, candles, params):
        if signal.decision_at.timestamp() >= start:
            anchors.append(int(signal.candle_at.timestamp()))
            sides.append(1 if signal.direction == "CALL" else -1)
    return Proposals(np.asarray(anchors, dtype=np.int64), np.asarray(sides, dtype=np.int8))


def returns(bars, periods):
    values = np.full((len(bars), len(periods)), np.nan)
    price = bars["c"]
    for index, period in enumerate(periods):
        values[period:, index] = price[period:] / price[:-period] - 1
    return Features(bars["t"], tuple(periods), values)


def _directional_rows(features, period):
    values = features.values
    scores = values[:, features.periods.index(period)]
    return scores, np.isfinite(values).all(axis=1) & (scores != 0)


def _directional_frame(features, scores, valid):
    signed = scores[valid]
    sides = np.sign(signed).astype(np.int8)
    raw = np.abs(signed)
    # The first column is the raw score; remaining returns are oriented to the proposed side.
    x = np.column_stack((raw, orient_columns(features.values[valid], sides)))
    return Frame(features.anchors[valid], sides, raw, x)


def directional(features, period):
    return _directional_frame(features, *_directional_rows(features, period))


def directional_volatility(features, period):
    if features.periods != VOLATILITY_PERIODS or period != VOLATILITY_SIDE_PERIOD:
        raise Invalid("score.volatility15", "volatilidade exige retornos [1, 5, 15] e lado R5")
    scores, valid = _directional_rows(features, period)
    volatility = trailing_population_std(features.values[:, 0], VOLATILITY_LOOKBACK)[valid]
    if not np.isfinite(volatility).all():
        raise Invalid("score.volatility15", "volatilidade não finita em linha elegível")
    frame = _directional_frame(features, scores, valid)
    return Frame(frame.anchors, frame.sides, frame.raw, np.column_stack((frame.x, volatility)))


def label_direction(bars, scores):
    times = bars["t"]
    targets = scores.anchors + ENTRY_SECONDS
    index = np.searchsorted(times, targets)
    valid = (index < len(times)) & (times[np.minimum(index, len(times) - 1)] == targets)
    y = np.full(len(index), -1, dtype=np.int8)
    picked = bars[index[valid]]
    y[valid] = np.sign(picked["c"] - picked["o"]) == scores.sides[valid]
    return Labels(scores.anchors, scores.anchors + LABEL_SECONDS, y)


def split(scores, labels, windows):
    if not np.array_equal(scores.anchors, labels.anchors):
        raise Invalid("split", "rótulos não correspondem ao score")
    partitions = {}
    for key, window in windows.items():
        start, end = int(window.start.timestamp()), int(window.end.timestamp())
        mask = (scores.anchors >= start) & (scores.anchors < end)
        if key == "train":
            mask &= (labels.available_at <= end) & (labels.y >= 0)
            partitions[key] = Training(scores.where(mask), labels.y[mask])
        else:
            partitions[key] = scores.where(mask)
        if not mask.any():
            raise Invalid(f"windows.{key}", "janela sem exemplos elegíveis")
    return partitions


def _cut(scores, rank):
    try:
        return rank_cutoff(scores, rank)
    except ValueError as exc:
        raise Invalid("rank", str(exc)) from exc


def train_edges(train, rank_pool, rank_core):
    score = train.frame.raw
    lo, hi = _cut(score, rank_pool), _cut(score, rank_core)
    return train.where((score > lo) & (score <= hi))


def fit_hgb(train, parameters):
    from ..adapters.registry import default_registry
    from ..adapters.sklearn_adapter import HGB_KEY

    resolved = default_registry().resolve(HGB_KEY)
    if not resolved.available:
        raise Invalid("learner.hgb", resolved.reason)
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return _fit_learner(resolved, train, parameters)


def resolve_xgboost():
    from ..adapters.registry import default_registry
    from ..adapters.xgboost_adapter import XGB_KEY

    resolved = default_registry().resolve(XGB_KEY)
    if not resolved.available:
        raise Invalid("learner.xgboost", resolved.reason)
    return resolved


def fit_xgboost(train, parameters):
    return _fit_learner(resolve_xgboost(), train, parameters)


def _fit_learner(resolved, train, parameters):
    adapter = resolved.adapter
    model = adapter.fit(train.frame.x, train.y, parameters)
    invocation = adapter.invocations[-1]
    receipt = {"adapter": resolved.key, "resolved_version": resolved.resolved_version,
               "samples": len(train.y), "wins": int(train.y.sum()),
               "seed": parameters["random_state"], "adapter_invocation": invocation.to_dict(),
               "library_lock": dict(resolved.library.provenance),
               "last_label_at": _stamp(train.frame.anchors.max() + LABEL_SECONDS)}
    return Fitted(adapter, model, receipt)


def predict_model(fitted, calibration, evaluation):
    return {name: Scored(frame, fitted.adapter.predict_proba(fitted.model, frame.x)[:, 1])
            for name, frame in (("calibration", calibration), ("evaluation", evaluation))}


def predict_hgb(fitted, calibration, evaluation):
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return predict_model(fitted, calibration, evaluation)


def _proposals(frame, mask, **metadata):
    return Proposals(frame.anchors[mask], frame.sides[mask], **metadata)


def raw_rank(calibration, evaluation, rank_budget, *, trace_study=False):
    cutoff = _cut(calibration.raw, rank_budget)
    mask = evaluation.raw > cutoff
    trace = None
    if trace_study:
        def selection(frame, proposed):
            reasons = np.where(proposed, "proposed_raw", "below_cutoff")
            return _SelectionTrace(frame, proposed, reasons)

        trace = _RankTrace(
            {"raw_cutoff": cutoff},
            selection(calibration, calibration.raw > cutoff),
            selection(evaluation, mask),
        )
    return _proposals(evaluation, mask, rank_fit=cutoff, trace=trace)


def edge_rank(calibration, evaluation, rank_budget, rank_pool, rank_core, *, trace_study=False):
    fitted = calibrate_edge_rank(
        calibration.frame.raw,
        calibration.probabilities,
        rank_budget=rank_budget,
        rank_pool=rank_pool,
        rank_core=rank_core,
    )
    masks = edge_rank_masks(evaluation.frame.raw, evaluation.probabilities, fitted)
    proposed = masks.proposed
    trace = None
    if trace_study:
        def selection(scored, applied, mask):
            reasons = np.select(
                [applied.core, applied.margin, applied.pool],
                ["proposed_core", "proposed_margin", "below_margin"],
                default="outside_pool",
            )
            return _SelectionTrace(scored.frame, mask, reasons, scored.probabilities)

        cal_masks = edge_rank_masks(calibration.frame.raw, calibration.probabilities, fitted)
        trace = _RankTrace(
            {"pool_cutoff": fitted.pool_cutoff, "core_cutoff": fitted.core_cutoff,
             "margin_score_cutoff": fitted.margin_score_cutoff,
             "margin_base_cutoff": fitted.margin_base_cutoff},
            selection(calibration, cal_masks, cal_masks.proposed),
            selection(evaluation, masks, proposed),
        )
    return _proposals(evaluation.frame, proposed, rank_fit=fitted, trace=trace)


def _stamp(timestamp):
    return datetime.fromtimestamp(int(timestamp), UTC).isoformat().replace("+00:00", "Z")


def _schedule(proposals, window):
    """Apply the M1 admission clock, independent of candles and outcomes."""
    start, end = int(window.start.timestamp()), int(window.end.timestamp())
    pending = None
    for anchor, side in zip(proposals.anchors, proposals.sides, strict=True):
        decision = anchor + DECISION_SECONDS
        if decision < start or anchor + LABEL_SECONDS > end:
            yield anchor, side, "outside_window", None
        elif pending is not None and decision < pending + LABEL_SECONDS:
            yield anchor, side, "blocked_overlap", pending
        else:
            pending = anchor
            yield anchor, side, "admitted", None


def _opportunity(asset, anchor, side):
    return {"asset": asset, "anchor_at": _stamp(anchor),
            "decision_at": _stamp(anchor + DECISION_SECONDS),
            "entry_at": _stamp(anchor + ENTRY_SECONDS),
            "expiry_at": _stamp(anchor + EXPIRY_SECONDS),
            "label_at": _stamp(anchor + LABEL_SECONDS),
            "direction": "CALL" if side == 1 else "PUT"}


def _outcome(candle, side):
    direction = price_movement(candle["o"], candle["c"])
    return "WIN" if direction == side else ("TIE" if direction == 0 else "LOSS")


def _trace_phase(snapshot, window, cutoffs, selection, decisions):
    frame = selection.frame
    bars = snapshot.bars
    entries = frame.anchors + ENTRY_SECONDS
    indexes = np.searchsorted(bars["t"], entries)
    end = int(window.end.timestamp())
    rows = []
    for i, (anchor, side) in enumerate(zip(frame.anchors, frame.sides, strict=True)):
        state, blocker, trade = decisions.get(anchor, (None, None, None))
        if trade is not None:
            row = dict(trade)
        else:
            row = _opportunity(snapshot.asset, anchor, side)
            # Retrospective only: never assign a label from later than this phase
            # or substitute the next candle for a missing exact entry candle.
            outcome = None
            index = indexes[i]
            if (anchor + LABEL_SECONDS <= end
                    and index < len(bars) and bars["t"][index] == entries[i]):
                candle = bars[index]
                if np.isfinite(candle["o"]) and np.isfinite(candle["c"]):
                    outcome = _outcome(candle, side)
            row["outcome"] = outcome
        row.update(
            raw_score=float(frame.raw[i]),
            probability_win=(None if selection.probabilities is None
                             else float(selection.probabilities[i])),
            selection=str(selection.reasons[i]), replay=state,
            blocking_anchor_at=None if blocker is None else _stamp(blocker),
        )
        rows.append(row)
    return PhaseTrace(dict(cutoffs), tuple(rows))


def replay(snapshot, proposals, window, *, calibration_window=None):
    admitted = []
    overlap = outside = 0
    rank_trace = proposals.trace
    decisions = {} if rank_trace is not None else None
    for anchor, side, state, blocker in _schedule(proposals, window):
        if state == "outside_window":
            outside += 1
        elif state == "blocked_overlap":
            overlap += 1
        else:
            admitted.append((anchor, side))
        if decisions is not None:
            decisions[anchor] = (state, blocker, None)

    # Only the completed evaluation schedule is settled. Verified snapshots
    # cover the exact entry candle of every admitted proposal.
    bars = snapshot.bars
    times = bars["t"]
    trades = []
    for anchor, side in admitted:
        index = np.searchsorted(times, anchor + ENTRY_SECONDS)
        trade = _opportunity(snapshot.asset, anchor, side)
        trade["outcome"] = _outcome(bars[index], side)
        trades.append(trade)
        if decisions is not None:
            decisions[anchor] = ("settled", None, trade)

    trace = None
    if rank_trace is not None:
        if calibration_window is None:
            raise Invalid("windows.calibration", "trilha de replay exige janela de calibração")
        calibration = rank_trace.calibration
        cal_proposals = _proposals(calibration.frame, calibration.proposed)
        cal_decisions = {
            anchor: (state, blocker, None)
            for anchor, _, state, blocker in _schedule(cal_proposals, calibration_window)
        }
        trace = {
            "calibration": _trace_phase(
                snapshot, calibration_window, rank_trace.cutoffs, calibration, cal_decisions,
            ),
            "evaluation": _trace_phase(
                snapshot, window, rank_trace.cutoffs, rank_trace.evaluation, decisions,
            ),
        }
    return Book(tuple(trades), len(proposals.anchors), overlap, outside, trace)


def compare(control, candidate, window, criteria):
    duration_seconds = int((window.end - window.start).total_seconds())

    def summarize(book):
        count = len(book.trades)
        wins = sum(row["outcome"] == "WIN" for row in book.trades)
        ties = sum(row["outcome"] == "TIE" for row in book.trades)
        winrate = Fraction(wins, count) if count else None
        trades_per_day = Fraction(count * 86400, duration_seconds)
        return {"proposed": book.proposed, "settled": count, "wins": wins, "ties": ties,
                "losses": count - wins - ties, "winrate": float(winrate) if winrate is not None else None,
                "trades_per_day": float(trades_per_day), "blocked_overlap": book.blocked_overlap,
                "outside_window": book.outside_window}, winrate, trades_per_day

    a, control_wr, _ = summarize(control)
    b, candidate_wr, candidate_tpd = summarize(candidate)
    improvement = None if control_wr is None or candidate_wr is None else (candidate_wr - control_wr) * 100
    thresholds = {key: Fraction(str(value)) for key, value in criteria.items()}
    gates = {
        "min_winrate": candidate_wr is not None and candidate_wr >= thresholds["min_winrate"],
        "min_trades_per_day": candidate_tpd >= thresholds["min_trades_per_day"],
        "min_improvement_pp": improvement is not None and improvement >= thresholds["min_improvement_pp"],
    }
    return Comparison(control, candidate, {"control": a, "candidate": b,
                                           "improvement_pp": float(improvement) if improvement is not None else None,
                                           "criteria_pass": gates, "meets_study_criteria": all(gates.values())})
