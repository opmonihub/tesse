"""Model cards, asset discovery and runtime provider contracts."""

from .assets import discover_assets
from .cards import ModelCard, card_hash, load_card, save_card, validate_runtime_key
from .providers import (
    LiveIndicatorsProvider,
    ModelProvider,
    ModelProviderRegistry,
    ProviderRegistry,
    UnavailableProvider,
)

__all__ = [
    "discover_assets", "ModelCard", "card_hash", "load_card", "save_card",
    "validate_runtime_key", "ModelProvider", "ModelProviderRegistry",
    "ProviderRegistry", "LiveIndicatorsProvider", "UnavailableProvider",
]

from ..model_provenance import ModelProvenance, provenance_from_mapping, provenance_hash

__all__ += ["ModelProvenance", "provenance_from_mapping", "provenance_hash",
            "RankEdgeM1Provider", "RANK_EDGE_RUNTIME_KEY", "provider_adoption_gate"]


def __getattr__(name):
    if name in {"RankEdgeM1Provider", "RANK_EDGE_RUNTIME_KEY", "provider_adoption_gate"}:
        from .. import providers

        return getattr(providers, name)
    raise AttributeError(name)
