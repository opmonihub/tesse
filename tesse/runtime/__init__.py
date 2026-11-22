"""Runtime activation, resolution and routing primitives."""
from .activation import activate_model_explicit, load_active, publish_active
from .active_snapshot import reconcile_active_snapshot, write_active_snapshot
from .resolver import ActiveModelResolver
from .routing import (
    ModelScore,
    RouterDecision,
    daily_challengers,
    eligible_for_activation,
    score_method_window,
    select_champion,
    wilson_lower_bound,
)

__all__ = [
    "reconcile_active_snapshot", "write_active_snapshot", "load_active",
    "publish_active", "activate_model_explicit", "ActiveModelResolver", "ModelScore", "RouterDecision",
    "daily_challengers", "eligible_for_activation", "score_method_window",
    "select_champion", "wilson_lower_bound",
]
