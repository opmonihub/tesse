"""Explicit runtime provider bridge for the canonical Rank-Edge system."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from .core import Signal
from .demo_dmi import RUNTIME_KEY as DEMO_DMI_RUNTIME_KEY
from .demo_dmi import DemoDmiProvider
from .models.cards import ModelCard, card_hash
from .models.providers import ModelProviderRegistry, UnavailableProvider

RANK_EDGE_RUNTIME_KEY = "tesse.rank_edge_m1_001@1.0.0"
RANK_EDGE_SYSTEM_ID = "RANK-EDGE-M1-001"


RANK_EDGE_ARTIFACT_FORMAT = "tesse.rank-edge.artifact"
RANK_EDGE_ARTIFACT_VERSION = 1


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RankEdgeArtifact:
    """Inert offline export for checking numeric and decision parity."""

    asset: str
    feature_order: tuple[str, ...]
    coefficients: tuple[float, ...]
    bias: float
    cutoff: float
    abstain_cutoff: float
    direction: str = "CALL"
    references: tuple[dict[str, Any], ...] = ()
    provenance: dict[str, Any] | None = None

    def __post_init__(self):
        if not isinstance(self.asset, str) or not self.asset.isupper() or not self.asset.isalnum():
            raise ValueError("Rank-Edge artifact asset is invalid")
        if not self.feature_order or len(self.feature_order) != len(self.coefficients):
            raise ValueError("Rank-Edge artifact feature/weight dimensions differ")
        if len(set(self.feature_order)) != len(self.feature_order):
            raise ValueError("Rank-Edge artifact feature order contains duplicates")
        if any(not isinstance(item, str) or not item or item.startswith("__") for item in self.feature_order):
            raise ValueError("Rank-Edge artifact feature names are invalid")
        numeric = (*self.coefficients, self.bias, self.cutoff, self.abstain_cutoff)
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in numeric):
            raise ValueError("Rank-Edge artifact numeric values are invalid")
        if not 0 <= self.cutoff <= 1 or not 0 <= self.abstain_cutoff <= 1:
            raise ValueError("Rank-Edge artifact cutoffs must be between 0 and 1")
        if self.direction not in {"CALL", "PUT"}:
            raise ValueError("Rank-Edge artifact direction is invalid")
        if not isinstance(self.references, tuple):
            object.__setattr__(self, "references", tuple(self.references or ()))
        for row in self.references:
            if not isinstance(row, dict) or not isinstance(row.get("features"), list):
                raise ValueError("Rank-Edge artifact reference is invalid")
            if len(row["features"]) != len(self.feature_order):
                raise ValueError("Rank-Edge artifact reference dimensions differ")
            if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in row["features"]):
                raise ValueError("Rank-Edge artifact reference contains invalid values")
            if row.get("direction") not in {None, "CALL", "PUT", "ABSTAIN"}:
                raise ValueError("Rank-Edge artifact reference decision is invalid")
            if row.get("expected_prediction") is not None:
                expected = row["expected_prediction"]
                if not isinstance(expected, (int, float)) or not math.isfinite(float(expected)):
                    raise ValueError("Rank-Edge reference prediction is invalid")
                actual = self.predict(row["features"])
                if abs(actual - float(expected)) > 1e-9:
                    raise ValueError("Rank-Edge prediction parity mismatch")
            expected_direction = row.get("expected_direction", row.get("expected_signal"))
            if expected_direction is not None and expected_direction != self.decide(row["features"]):
                raise ValueError("Rank-Edge signal parity mismatch")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, asset: str | None = None) -> "RankEdgeArtifact":
        if not isinstance(raw, Mapping) or raw.get("format") != RANK_EDGE_ARTIFACT_FORMAT:
            raise ValueError("unrecognized Rank-Edge artifact format")
        if raw.get("schema_version") != RANK_EDGE_ARTIFACT_VERSION:
            raise ValueError("unsupported Rank-Edge artifact schema")
        forbidden = ("loader", "module", "import", "callable", "pickle", "joblib", "path", "code")
        encoded = _canonical_json(raw).lower()
        if any(token.encode() in encoded for token in forbidden):
            raise ValueError("executable or path-bearing Rank-Edge artifact is not allowed")
        model = raw.get("model")
        if not isinstance(model, Mapping):
            raise ValueError("Rank-Edge artifact model is required")
        resolved_asset = raw.get("asset")
        if asset is not None and resolved_asset != asset:
            raise ValueError("Rank-Edge artifact asset differs")
        refs = raw.get("references", ())
        if refs is None:
            refs = ()
        if not isinstance(refs, list):
            raise ValueError("Rank-Edge artifact references must be a list")
        return cls(
            asset=resolved_asset,
            feature_order=tuple(raw.get("feature_order") or ()),
            coefficients=tuple(model.get("coefficients") or ()),
            bias=model.get("bias", 0.0),
            cutoff=model.get("cutoff", 0.5),
            abstain_cutoff=model.get("abstain_cutoff", 0.0),
            direction=model.get("direction", "CALL"),
            references=tuple(refs),
            provenance=dict(raw.get("provenance") or {}),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "format": RANK_EDGE_ARTIFACT_FORMAT,
            "schema_version": RANK_EDGE_ARTIFACT_VERSION,
            "asset": self.asset,
            "feature_order": list(self.feature_order),
            "model": {
                "coefficients": list(self.coefficients),
                "bias": self.bias,
                "cutoff": self.cutoff,
                "abstain_cutoff": self.abstain_cutoff,
                "direction": self.direction,
            },
            "references": list(self.references),
            "provenance": self.provenance or {},
        }

    def predict(self, features: Mapping[str, float] | list[float] | tuple[float, ...]) -> float:
        values = [features[name] for name in self.feature_order] if isinstance(features, Mapping) else list(features)
        if len(values) != len(self.coefficients):
            raise ValueError("Rank-Edge feature dimensions differ")
        if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
            raise ValueError("Rank-Edge features contain invalid values")
        score = self.bias + sum(float(weight) * float(value) for weight, value in zip(self.coefficients, values))
        return 1.0 / (1.0 + math.exp(-max(-700.0, min(700.0, score))))

    def decide(self, features: Mapping[str, float] | list[float] | tuple[float, ...]) -> str | None:
        probability = self.predict(features)
        if probability < self.abstain_cutoff or probability < self.cutoff:
            return None
        return self.direction


def export_rank_edge_artifact(artifact: RankEdgeArtifact | Mapping[str, Any]) -> bytes:
    value = artifact if isinstance(artifact, RankEdgeArtifact) else RankEdgeArtifact.from_mapping(artifact)
    return _canonical_json(value.to_mapping())


def validate_rank_edge_artifact(raw: bytes | Mapping[str, Any], *, asset: str | None = None) -> RankEdgeArtifact:
    if isinstance(raw, bytes):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Rank-Edge artifact must be canonical JSON") from exc
    else:
        value = raw
    return RankEdgeArtifact.from_mapping(value, asset=asset)


def load_rank_edge_operational_artifact(raw: bytes, *, asset: str | None = None) -> Any:
    validate_rank_edge_artifact(raw, asset=asset)
    raise RuntimeError("Rank-Edge export does not define verified market inference")


class RankEdgeM1Provider:
    runtime_key = RANK_EDGE_RUNTIME_KEY
    system_id = RANK_EDGE_SYSTEM_ID
    timeframe = "M1"

    def __init__(
        self,
        *,
        model: Any = None,
        loader: Callable[[bytes], Any] | None = None,
        expected_asset: str | None = None,
    ):
        self.model, self.loader, self.expected_asset = model, loader, expected_asset
        self._weights_hash = self._card_hash = None

    def _validate_card(self, card: ModelCard, asset: str | None = None) -> None:
        runtime_key = (
            getattr(card, "runtime_key", None) or (card.environment or {}).get("runtime_key")
            if isinstance(card, ModelCard)
            else None
        )
        if (
            not isinstance(card, ModelCard)
            or card.system_id not in (None, self.system_id)
            or runtime_key != self.runtime_key
            or (card.timeframe not in (None, self.timeframe))
        ):
            raise ValueError("provider/card identity mismatch")
        if any(
            target is not None and card.asset != target for target in (asset, self.expected_asset)
        ):
            raise ValueError("provider asset differs")

    def validate_card_identity(self, card: ModelCard) -> bool:
        """Validate a canonical card before an artifact has been loaded.

        Promotion uses this check to prove that the installed runtime owns the
        card's versioned key. Runtime health remains stricter and still requires
        verified weights and a scan-capable model.
        """
        self._validate_card(card)
        return True

    def is_healthy(self, card: ModelCard) -> bool:
        try:
            self._validate_card(card)
            return (
                self._card_hash == card_hash(card)
                and self._weights_hash == card.weights_sha256
                and self.model is not None
                and callable(getattr(self.model, "scan", None))
            )
        except (TypeError, ValueError):
            return False

    health_check = is_healthy

    def load_weights(self, card: ModelCard, raw: bytes) -> Any:
        # Loading is transactional: a failed or unavailable loader must not
        # leave a previously injected model paired with a newly claimed hash.
        self.model = None
        self._weights_hash = self._card_hash = None
        self._validate_card(card)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != card.weights_sha256:
            raise ValueError("weights hash differs from card")
        if self.loader is None:
            # Generic external bundles are intentionally unavailable by default.
            raise RuntimeError("Rank-Edge runtime loader unavailable")
        model = self.loader(raw)
        self.model = model
        self._weights_hash = digest
        self._card_hash = card_hash(card)
        return self.model

    async def scan(
        self, client: Any, asset: str, card: ModelCard, version: str, params: Any, boundary: Any
    ) -> Signal | None:
        self._validate_card(card, asset)
        # A model object is usable only when it was loaded from the exact
        # artifact named by the card.  This keeps scan() aligned with the
        # registry health check and prevents stale or injected models.
        if not self.is_healthy(card):
            return None
        scan = getattr(self.model, "scan", None)
        if not callable(scan):
            return None
        result = scan(client, asset, card, version, params, boundary)
        if hasattr(result, "__await__"):
            result = await result
        if result is None:
            return None
        if not isinstance(result, Signal) or result.symbol != asset:
            raise ValueError("provider signal identity mismatch")
        return replace(result, model_id=card.model_id, card_sha256=card_hash(card))


def build_runtime_provider_registry(
    cards: Any = (),
    *,
    loaders: Any = None,
    weights: Any = None,
    models: Any = None,
    operational: bool = False,
) -> ModelProviderRegistry:
    """Build an exact-key registry from verified cards and runtime artifacts.

    A provider is registered only after its model has been loaded and its
    synchronous health check succeeds. Missing loaders or weights therefore
    produce an empty registry, making the worker abstain explicitly.
    """
    registry = ModelProviderRegistry()
    if isinstance(cards, ModelCard):
        cards = (cards,)
    elif isinstance(cards, dict):
        cards = tuple(cards.values())
    for card in cards or ():
        runtime_key = (
            (getattr(card, "runtime_key", None) or (card.environment or {}).get("runtime_key"))
            if isinstance(card, ModelCard)
            else None
        )
        if not isinstance(card, ModelCard):
            continue
        asset = card.asset
        loader = loaders.get(asset) if isinstance(loaders, dict) else loaders
        raw = weights.get(asset) if isinstance(weights, dict) else weights
        model = models.get(asset) if isinstance(models, dict) else models
        if runtime_key == DEMO_DMI_RUNTIME_KEY:
            if model is None and isinstance(raw, bytes):
                try:
                    registry.register(DemoDmiProvider().load(card, raw))
                except ValueError:
                    pass
            continue
        if runtime_key != RANK_EDGE_RUNTIME_KEY:
            continue
        # Every registered model must be backed by the card-declared artifact.
        # Supplying an already-instantiated model never bypasses this check.
        # It is rejected outright because there is no safe way to prove that
        # the object came from the card-declared bytes.
        if model is not None:
            continue
        if not isinstance(raw, (bytes, bytearray)):
            continue
        if loader is load_rank_edge_artifact or (operational and loader is None):
            def scoped_loader(value, _asset=asset):
                return load_rank_edge_artifact(value, operational=operational, asset=_asset)
            loader = scoped_loader
        provider = RankEdgeM1Provider(loader=loader, expected_asset=asset)
        try:
            provider.load_weights(card, bytes(raw))
        except Exception:
            continue
        if provider.is_healthy(card):
            registry.register(provider)
    return registry


class _FixtureSignalModel:
    """Small, deterministic model used by offline integration fixtures.

    It only emits signals explicitly present in the verified JSON artifact. It
    never derives weights or falls back to indicators, so an absent/unknown
    artifact still leaves the provider unavailable.
    """

    def __init__(self, payload: dict[str, Any]):
        entries = payload.get("signals")
        if not isinstance(entries, list):
            raise ValueError("fixture artifact requires a signals list")
        self.entries = tuple(entries)

    def scan(
        self, client: Any, asset: str, card: ModelCard, version: str, params: Any, boundary: Any
    ) -> Signal | None:
        boundary_iso = boundary.isoformat() if hasattr(boundary, "isoformat") else None
        for item in self.entries:
            if not isinstance(item, dict) or item.get("symbol", asset) != asset:
                continue
            candle_at = item.get("candle_at")
            if not isinstance(candle_at, str):
                continue
            if boundary_iso is not None and candle_at not in {boundary_iso, str(boundary_iso)}:
                # Fixtures may key the closed candle one minute before the
                # broker boundary, which is the worker's signal convention.
                try:
                    if datetime.fromisoformat(candle_at) != boundary - timedelta(minutes=1):
                        continue
                except (TypeError, ValueError):
                    continue
            return Signal(
                asset,
                datetime.fromisoformat(candle_at),
                item.get("direction"),
                float(item.get("adx")),
                float(item.get("distance")),
                confidence=float(item["confidence"])
                if item.get("confidence") is not None
                else None,
            )
        return None


def load_rank_edge_artifact(
    raw: bytes, *, operational: bool = False, asset: str | None = None
) -> Any:
    """Decode a Rank-Edge export, or a signal fixture in offline mode only."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Rank-Edge artifact loader unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Rank-Edge artifact must be a mapping")
    if payload.get("format") == RANK_EDGE_ARTIFACT_FORMAT:
        if operational:
            return load_rank_edge_operational_artifact(raw, asset=asset)
        return validate_rank_edge_artifact(payload, asset=asset)
    if operational:
        raise RuntimeError("fixture Rank-Edge artifacts are not allowed in operational runtime")
    return _FixtureSignalModel(payload)


def build_runtime_provider_registry_from_arsenal(
    arsenal: Any,
    store: Any,
    *,
    loaders: Mapping[str, Callable[[bytes], Any]] | None = None,
    operational: bool = False,
) -> ModelProviderRegistry:
    """Rebuild healthy providers from Store pointers and verified artifacts."""
    cards: dict[str, ModelCard] = {}
    weights: dict[str, bytes] = {}
    resolved_loaders: dict[str, Callable[[bytes], Any]] = {}
    for pointer in store.active_pointers():
        asset = pointer["asset"]
        try:
            from .runtime.resolver import verified_model_card

            metadata = store.get_model_metadata(pointer["model_id"], pointer["model_version"])
            if (
                metadata is None
                or metadata["status"] != "active"
                or pointer["model_version"] != metadata["model_version"]
            ):
                continue
            card = verified_model_card(arsenal, pointer["model_id"], asset=asset, metadata=metadata)
            raw = arsenal.load_weights(card, asset)
        except Exception:
            continue
        key = card.runtime_key or card.provider
        loader = None
        if isinstance(loaders, dict):
            loader = loaders.get(asset) or loaders.get(key)
        if loader is None and key == RANK_EDGE_RUNTIME_KEY:
            loader = load_rank_edge_artifact
        if loader is None and key != DEMO_DMI_RUNTIME_KEY:
            continue
        cards[asset], weights[asset], resolved_loaders[asset] = card, raw, loader
    return build_runtime_provider_registry(
        cards, loaders=resolved_loaders, weights=weights, operational=operational
    )


def rank_edge_provider(**kwargs: Any) -> RankEdgeM1Provider:
    return RankEdgeM1Provider(**kwargs)


def unavailable_rank_edge(reason: str = "runtime provider unavailable") -> UnavailableProvider:
    return UnavailableProvider(RANK_EDGE_RUNTIME_KEY, reason)


def provider_adoption_gate(
    card: ModelCard, *, resolver: Any = None, runtime_key: str | None = None
) -> tuple[bool, str]:
    try:
        key = runtime_key or card.runtime_key
        if key != RANK_EDGE_RUNTIME_KEY:
            return False, "runtime_key is not the Rank-Edge provider"
        if resolver is None:
            return False, "provider resolver is required"
        validator = getattr(resolver, "validate_card", None)
        if callable(validator):
            return (True, "ok") if validator(card) else (False, "provider unavailable or unhealthy")
        provider = (
            resolver.resolve_key(key, card)
            if hasattr(resolver, "resolve_key")
            else resolver.resolve(card)
        )
        if provider is None:
            return False, "provider unavailable or unhealthy"
        return True, "ok"
    except Exception as exc:
        return False, f"provider adoption failed: {type(exc).__name__}"
