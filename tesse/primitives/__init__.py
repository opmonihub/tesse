"""Canonical reusable Tesse primitives."""

from .contracts import get_primitive
from .features import (
    contrast_group,
    contrast_groups,
    orient_columns,
    orient_directional_columns,
)
from .ranking import (
    EdgeRankCalibration,
    apply_edge_rank,
    apply_rank,
    calibrate_edge_rank,
    calibrate_rank,
    cutoff_by_rank,
    rank_cutoff,
    raw_rank_mask,
)

__all__ = [
    "EdgeRankCalibration",
    "apply_edge_rank",
    "apply_rank",
    "calibrate_edge_rank",
    "calibrate_rank",
    "contrast_group",
    "contrast_groups",
    "cutoff_by_rank",
    "get_primitive",
    "orient_columns",
    "orient_directional_columns",
    "rank_cutoff",
    "raw_rank_mask",
]
