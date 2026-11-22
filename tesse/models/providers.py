"""Read-only active model resolution and explicit runtime providers."""
from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any, Protocol

from ..models.cards import ModelCard, card_hash, validate_runtime_key


class ModelProvider(Protocol):
    runtime_key: str

    def is_healthy(self, card: ModelCard) -> bool: ...


class ModelProviderRegistry:
    """Strict exact-key provider registry.

    Inventory/library keys and legacy recipe IDs are never searched as fallbacks.
    Health checks are synchronous: an awaitable result is unavailable rather
    than being executed from a read-only resolver.
    """

    def __init__(self):
        self._providers: dict[tuple[str, str | None], Any] = {}

    def register(self, provider: Any, *, runtime_key: str | None = None, asset: str | None = None) -> Any:
        key = runtime_key or getattr(provider, "runtime_key", None)
        if not isinstance(key, str) or not key or key != getattr(provider, "runtime_key", None):
            raise ValueError("provider runtime_key must match the registered key")
        scoped_asset = getattr(provider, "expected_asset", None)
        if scoped_asset is not None and asset is not None and asset != scoped_asset:
            raise ValueError("provider asset differs from registration")
        asset = asset or scoped_asset
        if asset is not None and (not asset.isalnum() or not asset.isupper()):
            raise ValueError("invalid provider asset")
        identity = (key, asset)
        if identity in self._providers or (
            asset is None and any(registered_key == key for registered_key, _ in self._providers)
        ) or (asset is not None and (key, None) in self._providers):
            raise ValueError(f"provider key already registered or ambiguous: {identity}")
        self._providers[identity] = provider
        return provider

    def get(self, key: str, *, asset: str | None = None) -> Any | None:
        if asset is not None:
            return self._providers.get((key, asset)) or self._providers.get((key, None))
        matches = [provider for (runtime, _), provider in self._providers.items() if runtime == key]
        return matches[0] if len(matches) == 1 else None

    def resolve_key(self, runtime_key: str, card: ModelCard | None = None) -> Any | None:
        if not isinstance(runtime_key, str):
            return None
        asset = card.asset if card is not None else None
        provider = self.get(runtime_key, asset=asset)
        if provider is None:
            return None
        health = getattr(provider, "is_healthy", None) or getattr(provider, "health_check", None)
        if health is None:
            return None
        try:
            result = health(card) if card is not None else health()
            if inspect.isawaitable(result):
                return None
            if hasattr(result, "available"):
                return provider if result.available else None
            return provider if result else None
        except Exception:
            return None

    def validate_card(self, card: ModelCard) -> bool:
        """Validate provider/card ownership without claiming runtime health.

        Promotion calls this before the artifact exists. Runtime resolution
        continues to use ``resolve`` and therefore still requires loaded,
        hash-matched weights and a healthy provider.
        """
        runtime_key = getattr(card, "runtime_key", None)
        if not runtime_key and isinstance(getattr(card, "environment", None), dict):
            runtime_key = card.environment.get("runtime_key")
        provider = self.get(runtime_key, asset=card.asset)
        validator = getattr(provider, "validate_card_identity", None) if provider is not None else None
        if not callable(validator):
            return False
        try:
            return bool(validator(card))
        except Exception:
            return False

    def resolve(self, card: ModelCard) -> Any | None:
        runtime_key = getattr(card, "runtime_key", None)
        if not runtime_key:
            environment = card.environment if isinstance(card.environment, dict) else {}
            runtime_key = environment.get("runtime_key")
        if not isinstance(runtime_key, str) or runtime_key in {"unknown", ""}:
            return None
        if card.is_canonical:
            try:
                validate_runtime_key(runtime_key)
            except ValueError:
                return None
        return self.resolve_key(runtime_key, card)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted({key for key, _ in self._providers}))


class LiveIndicatorsProvider:
    """Explicit DMI provider retained for historical/research callers."""

    runtime_key = "tesse.dmi@1.0.0"

    def __init__(self, indicators: Any):
        self.indicators = indicators

    def is_healthy(self, card: ModelCard) -> bool:
        key = getattr(card, "runtime_key", None) or (card.environment or {}).get("runtime_key")
        return key == self.runtime_key

    async def scan(self, client, asset, card, version, params, boundary):
        signal = await self.indicators.scan(client, asset, version, params, boundary,
                                            model_id=card.model_id)
        if signal is None:
            return None
        if signal.symbol != asset:
            raise ValueError("provider signal asset differs")
        return replace(signal, model_id=card.model_id, card_sha256=card_hash(card))


class UnavailableProvider:
    """Typed provider used when an installed runtime is not available."""

    def __init__(self, runtime_key: str, reason: str):
        self.runtime_key, self.reason = runtime_key, reason

    def is_healthy(self, card: ModelCard) -> bool:
        return False

    async def scan(self, *args, **kwargs):
        return None

# Compatibility name for callers that treat the registry as a provider catalog.
ProviderRegistry = ModelProviderRegistry
