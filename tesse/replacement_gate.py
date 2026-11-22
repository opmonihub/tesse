"""Equivalence gate for replacing retired pilot coverage.

A pilot may be removed only after its replacement has a declared coverage set,
matching replay fixtures and a passing rollback drill.  The gate is pure and
read-only so CI/review can run it before deleting legacy paths.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REQUIRED = frozenset({"pilot", "replacement", "coverage", "replay_passed", "rollback_passed", "approved"})


def validate_replacement_gate(gate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(gate, Mapping):
        raise ValueError("replacement gate must be a mapping")
    missing = sorted(REQUIRED - set(gate))
    if missing:
        raise ValueError(f"replacement gate missing: {', '.join(missing)}")
    pilot, replacement = gate["pilot"], gate["replacement"]
    if not isinstance(pilot, str) or not pilot or not isinstance(replacement, str) or not replacement:
        raise ValueError("replacement gate identities must be non-empty strings")
    if pilot == replacement:
        raise ValueError("replacement gate must name distinct pilot and replacement")
    coverage = gate["coverage"]
    if not isinstance(coverage, Sequence) or isinstance(coverage, (str, bytes)) or not coverage:
        raise ValueError("replacement gate coverage must be non-empty")
    if any(not isinstance(item, str) or not item for item in coverage):
        raise ValueError("replacement gate coverage entries must be strings")
    for flag in ("replay_passed", "rollback_passed", "approved"):
        if gate[flag] is not True:
            raise ValueError(f"replacement gate requires {flag}=true")
    return {
        "pilot": pilot,
        "replacement": replacement,
        "coverage": list(dict.fromkeys(coverage)),
        "replay_passed": True,
        "rollback_passed": True,
        "approved": True,
    }


def _canonical_namespaces_present() -> bool:
    package = Path(__file__).parent
    current = ("models", "registry", "runtime", "execution")
    retired = ("model_arsenal", "model_router", "model_runtime", "model_activation", "execution")
    return (all((package / module / "__init__.py").is_file() for module in current)
            and not any((package / f"{module}.py").exists() for module in retired))


def _public_namespaces_import() -> bool:
    from . import models, registry, runtime
    from .execution import Executor

    return all((models.ModelCard, registry.CatalogRegistry, runtime.ActiveModelResolver, Executor))


_CHECKS = {
    "canonical_namespaces_present": _canonical_namespaces_present,
    "public_namespaces_import": _public_namespaces_import,
}


def can_remove_pilot(gate: Mapping[str, Any]) -> bool:
    try:
        validate_replacement_gate(gate)
        return executable_evidence(gate)["passed"]
    except (ImportError, OSError, ValueError):
        return False


def executable_evidence(gate):
    """Run trusted local checks. Inputs cannot select paths or arbitrary imports."""
    if not isinstance(gate, Mapping):
        raise ValueError("replacement gate must be a mapping")
    checks = gate.get("checks")
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)) or not checks:
        raise ValueError("replacement gate requires executable checks")
    if any(not isinstance(check, str) or check not in _CHECKS for check in checks):
        raise ValueError("replacement check must be a registered check ID")
    results = [{"check": check, "passed": _CHECKS[check]() is True} for check in checks]
    return {"checks": results, "passed": all(item["passed"] for item in results), "writes": 0}
