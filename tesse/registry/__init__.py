"""Canonical catalog and model registry APIs."""
from .catalog import CatalogRegistry, RegistryRecord
from .identity import (
    BEHAVIORS,
    FAMILIES,
    TIMEFRAMES,
    ModelIdentity,
    SystemIdentity,
    validate_model_id,
    validate_system_id,
    validate_version,
)
from .models import ModelArtifactRegistry
from .promotion import (
    ResearchBundle,
    approved_bundle_diagnostic,
    assert_operation_unchanged,
    diagnose_approved_bundle,
    promote_research_bundle,
    rollback_model,
    validate_research_bundle,
)

__all__ = [
    "BEHAVIORS", "FAMILIES", "TIMEFRAMES", "ModelIdentity", "SystemIdentity",
    "validate_model_id", "validate_system_id", "validate_version",
    "CatalogRegistry", "RegistryRecord", "ModelArtifactRegistry", "ResearchBundle",
    "assert_operation_unchanged", "promote_research_bundle", "rollback_model",
    "validate_research_bundle", "diagnose_approved_bundle", "approved_bundle_diagnostic",
]
