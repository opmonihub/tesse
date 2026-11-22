"""Execution boundary for broker order submission and pilot progression.

The readiness drill is dependency-light and remains importable before optional
broker SDK dependencies are installed. Executor symbols are loaded lazily.
"""

from .readiness import readiness_report

__all__ = ["Executor", "advance_pilot", "eligible_assets", "readiness_report", "plan_test_drill", "authorize_test_drill", "publish_test_receipt", "record_recovery"]


def __getattr__(name):
    if name in {"Executor", "advance_pilot", "eligible_assets"}:
        from .executor import Executor, advance_pilot, eligible_assets

        return {"Executor": Executor, "advance_pilot": advance_pilot, "eligible_assets": eligible_assets}[name]
    if name in {"plan_test_drill", "authorize_test_drill", "publish_test_receipt", "record_recovery"}:
        return _drill_symbol(name)
    raise AttributeError(name)


def _drill_symbol(name):
    from .drill import authorize_test_drill, plan_test_drill, publish_test_receipt, record_recovery
    return {"plan_test_drill": plan_test_drill, "authorize_test_drill": authorize_test_drill,
            "publish_test_receipt": publish_test_receipt, "record_recovery": record_recovery}[name]
