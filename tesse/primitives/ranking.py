"""Reusable score-only rank selection primitives.

The functions in this module deliberately know nothing about estimators, Forge
frames, brokers, or strategy identities.  Calibration produces an immutable
boundary from calibration scores; application evaluates new scores against
that frozen boundary without refitting.
"""

from dataclasses import dataclass

import numpy as np


def _scores(value, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.ndim != 1:
        raise ValueError(f"{name} must be a one-dimensional score array")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must contain only finite scores")
    return result


def _rank(value, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def rank_cutoff(scores, rank: int) -> float:
    """Return the strict cutoff for a descending rank pool.

    A rank of ``n`` needs at least one additional observation to define the
    strict ``> cutoff`` pool.  Equal scores remain subject to the caller's
    strict comparison and are therefore not silently promoted.
    """

    values = _scores(scores, "scores")
    rank = _rank(rank, "rank")
    if len(values) <= rank:
        raise ValueError(f"insufficient or invalid sample for rank {rank}")
    index = len(values) - rank - 1
    return float(np.partition(values, index)[index])


# Short aliases make the primitive convenient for callers that use "cut" as
# the domain term while keeping rank_cutoff as the documented spelling.
cutoff_by_rank = rank_cutoff


def _pool_core(base, pool_cutoff, core_cutoff):
    pool = base > pool_cutoff
    return pool, pool & (base > core_cutoff)


@dataclass(frozen=True, slots=True)
class EdgeRankCalibration:
    """Frozen cutoffs learned only from a calibration window."""

    pool_cutoff: float
    core_cutoff: float
    margin_score_cutoff: float
    margin_base_cutoff: float
    rank_pool: int
    rank_core: int
    rank_budget: int
    core_count: int

    @property
    def boundary(self) -> tuple[float, float]:
        return self.margin_score_cutoff, self.margin_base_cutoff

    # Compatibility spellings for callers that name the values as cuts rather
    # than cutoffs; all are immutable views of the same calibration.
    @property
    def pool_cut(self) -> float:
        return self.pool_cutoff

    @property
    def core_cut(self) -> float:
        return self.core_cutoff

    @property
    def secondary_cut(self) -> float:
        return self.margin_score_cutoff

    @property
    def base_cut(self) -> float:
        return self.margin_base_cutoff


def calibrate_edge_rank(
    base_scores,
    secondary_scores,
    rank_budget: int,
    rank_pool: int,
    rank_core: int,
) -> EdgeRankCalibration:
    """Fit edge-rank cutoffs from calibration scores only.

    ``base_scores`` define the raw rank pool and protected core.  Within the
    pool but outside the core, ``secondary_scores`` define the marginal
    boundary, with ``base_scores`` as the deterministic tie-breaker.
    """

    base = _scores(base_scores, "base_scores")
    secondary = _scores(secondary_scores, "secondary_scores")
    if base.shape != secondary.shape:
        raise ValueError("base_scores and secondary_scores must have equal shape")
    rank_pool = _rank(rank_pool, "rank_pool")
    rank_core = _rank(rank_core, "rank_core")
    rank_budget = _rank(rank_budget, "rank_budget")
    if not rank_core < rank_budget < rank_pool:
        raise ValueError("expected rank_core < rank_budget < rank_pool")

    pool_cutoff = rank_cutoff(base, rank_pool)
    core_cutoff = rank_cutoff(base, rank_core)
    pool, core = _pool_core(base, pool_cutoff, core_cutoff)
    core_count = int(core.sum())
    if core_count >= rank_budget:
        raise ValueError("protected core consumes the complete budget")

    available = pool & ~core
    quota = rank_budget - core_count
    if int(available.sum()) <= quota:
        raise ValueError("edge boundary requires more candidates than quota")
    finite = available & np.isfinite(base) & np.isfinite(secondary)
    if int(finite.sum()) <= quota:
        raise ValueError("edge boundary requires finite candidates")

    # lexsort uses the last key as primary: secondary first, base as the
    # deterministic tie-breaker.  The element just below the quota is the
    # strict boundary, matching the historical kernel.
    order = np.lexsort((base[finite], secondary[finite]))
    chosen = np.flatnonzero(finite)[order[-quota - 1]]
    return EdgeRankCalibration(
        pool_cutoff=float(pool_cutoff),
        core_cutoff=float(core_cutoff),
        margin_score_cutoff=float(secondary[chosen]),
        margin_base_cutoff=float(base[chosen]),
        rank_pool=rank_pool,
        rank_core=rank_core,
        rank_budget=rank_budget,
        core_count=core_count,
    )


# Calibration is also a useful noun in call sites and keeps the public API
# discoverable without tying it to an estimator.
calibrate_rank = calibrate_edge_rank


@dataclass(frozen=True, slots=True)
class EdgeRankMasks:
    pool: np.ndarray
    core: np.ndarray
    margin: np.ndarray

    @property
    def proposed(self) -> np.ndarray:
        return self.core | self.margin


def edge_rank_masks(base_scores, secondary_scores, calibration: EdgeRankCalibration) -> EdgeRankMasks:
    """Apply frozen edge-rank cuts; core and admitted margin are disjoint."""

    if not isinstance(calibration, EdgeRankCalibration):
        raise TypeError("calibration must be an EdgeRankCalibration")
    base = _scores(base_scores, "base_scores")
    secondary = _scores(secondary_scores, "secondary_scores")
    if base.shape != secondary.shape:
        raise ValueError("base_scores and secondary_scores must have equal shape")
    marginal = (secondary > calibration.margin_score_cutoff) | (
        (secondary == calibration.margin_score_cutoff)
        & (base > calibration.margin_base_cutoff)
    )
    pool, core = _pool_core(base, calibration.pool_cutoff, calibration.core_cutoff)
    return EdgeRankMasks(pool, core, pool & ~core & marginal)


def apply_edge_rank(base_scores, secondary_scores, calibration: EdgeRankCalibration) -> np.ndarray:
    """Apply frozen edge-rank cutoffs to evaluation scores."""

    return edge_rank_masks(base_scores, secondary_scores, calibration).proposed


apply_rank = apply_edge_rank


def raw_rank_mask(base_scores, rank_budget: int) -> np.ndarray:
    """Return the strict raw-score rank mask used by the control arm."""

    values = _scores(base_scores, "base_scores")
    return values > rank_cutoff(values, rank_budget)
