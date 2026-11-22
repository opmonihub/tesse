"""Validated, staged and idempotent research-to-model promotion."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..model_provenance import provenance_from_mapping, provenance_hash, require_adopted_library
from ..models.cards import ModelCard, card_hash, validate_runtime_key

_REQUIRED = (
    "run_id",
    "system_id",
    "system_version",
    "assets",
    "timeframe",
    "cutoff_utc",
    "data_hash",
    "evidence",
    "weights",
    "adapter_provenance",
)


def diagnose_approved_bundle(source: str | Path, *, system_id: str, asset: str) -> dict[str, str]:
    """Return a stable readiness diagnostic for the official bundle source.

    The catalog's historical recipe is not an approved research bundle.  A
    missing source therefore gets an explicit blocking result instead of a
    fabricated model artifact.  Existing sources are checked for identity so
    a bundle for another asset cannot be mistaken for the BTCUSDT source.
    """
    path = Path(source)
    if not path.is_file():
        return {
            "status": "blocked",
            "reason": "missing_approved_bundle",
            "system_id": system_id,
            "asset": asset,
        }
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("approved bundle must be a mapping")
    if raw.get("system_id") != system_id:
        raise ValueError("approved bundle system_id differs")
    assets = raw.get("assets")
    normalized = (assets,) if isinstance(assets, str) else tuple(assets or ())
    if asset not in normalized:
        raise ValueError("approved bundle asset coverage differs")
    return {
        "status": "available",
        "reason": "approved_bundle_available",
        "system_id": system_id,
        "asset": asset,
    }


# Descriptive alias for callers that use noun-first naming.
approved_bundle_diagnostic = diagnose_approved_bundle


def _sha(value: Any) -> str:
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value.lower())
    ):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return value


def _evidence_digest(value: Any, label: str) -> str:
    """Validate a digest carried by a sealed evidence reference.

    Evidence is an artifact identity, rather than a boolean assessment.  Keep
    this check separate from weight/data hashes so callers cannot accidentally
    satisfy one gate with a value intended for another artifact.
    """
    return _digest(value, label)


def _validate_evidence_reference(evidence: Mapping[str, Any], run_id: str) -> None:
    """Require an evidence reference to identify the run it came from.

    Older hand-authored fixtures only contained ``holdout`` metrics.  They are
    deliberately rejected for promotion: a metric payload without a run ID and
    immutable artifact hash cannot establish which run was reviewed.
    """
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence is required")
    holdout = evidence.get("holdout") or evidence.get("oos") or evidence.get("out_of_sample")
    if not isinstance(holdout, Mapping) or not holdout:
        raise ValueError("evidence requires holdout/oos evidence")
    linked_run = evidence.get("run_id") or evidence.get("research_run_id")
    if linked_run != run_id:
        raise ValueError("evidence.run_id must match bundle run_id")
    digest = evidence.get("sha256") or evidence.get("evidence_sha256") or evidence.get("hash")
    if digest is None:
        raise ValueError("evidence.sha256 is required")
    _evidence_digest(digest, "evidence.sha256")


def _validate_adapter_provenance(raw: Mapping[str, Any]):
    """Resolve the concrete capability and its installed lock version.

    Library inventory IDs are intentionally not capability IDs.  A promotion
    therefore has to resolve the versioned adapter key through the same
    registry used by Forge and compare the installed package version with the
    catalog lock before a card is created.
    """
    provenance = provenance_from_mapping(raw)
    suffix = provenance.adapter_key.rsplit("@", 1)[1]
    if provenance.adapter_version != suffix:
        raise ValueError("adapter_version differs from adapter_key")
    try:
        from ..adapters.registry import default_registry

        resolution = default_registry().resolve(provenance.adapter_key)
    except (OSError, ValueError) as exc:
        raise ValueError(f"adapter catalog unavailable: {exc}") from exc
    if not resolution.available:
        raise ValueError(f"adapter_key unavailable: {resolution.reason}")
    if resolution.library is None or resolution.library.id != provenance.library_id:
        raise ValueError("adapter_key capability differs from library_id")
    installed = resolution.resolved_version
    if installed != provenance.resolved_package.split("==")[-1]:
        raise ValueError(
            f"installed adapter version {installed} differs from bundle {provenance.resolved_package}"
        )
    locked = resolution.library.provenance.get("resolved_version")
    if locked != installed:
        raise ValueError(f"installed adapter version {installed} differs from catalog lock {locked}")
    return provenance


def _staging_dir(root: str | Path, operation_id: str) -> Path:
    if not isinstance(operation_id, str) or not operation_id or Path(operation_id).is_absolute():
        raise ValueError("operation_id must be a single safe path component")
    path = Path(operation_id)
    if len(path.parts) != 1 or path.name in {".", ".."} or path.name != operation_id:
        raise ValueError("operation_id must be a single safe path component")
    staging = Path(root) / ".staging"
    target = (staging / operation_id).resolve()
    if target.parent != staging.resolve():
        raise ValueError("operation_id escapes staging")
    return target


@dataclass(frozen=True, slots=True)
class ResearchBundle:
    run_id: str
    system_id: str
    system_version: str
    assets: tuple[str, ...]
    timeframe: str
    cutoff_utc: str
    data_hash: str
    evidence: Mapping[str, Any]
    weights: Mapping[str, Any]
    adapter_provenance: Mapping[str, Any]
    submitted_definition: Mapping[str, Any] = field(default_factory=dict)
    effective_definition: Mapping[str, Any] = field(default_factory=dict)
    engine_identity: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResearchBundle":
        if not isinstance(raw, Mapping):
            raise ValueError("research bundle must be a mapping")
        # Forge run manifests are portable evidence receipts, not promotion
        # bundles.  Refuse the shape explicitly instead of reporting a vague
        # missing-field error or treating a passing verdict as authorization.
        if raw.get("status") == "completed" and raw.get("result_kind") in {"comparison", "predictions"}:
            raise ValueError(
                "Forge run evidence is not a research bundle; provide a sealed bundle with weights and provenance"
            )
        missing = [k for k in _REQUIRED if k not in raw]
        if missing:
            raise ValueError("research bundle missing: " + ", ".join(missing))
        if not isinstance(raw["run_id"], str) or not raw["run_id"].strip():
            raise ValueError("run_id is required")
        assets = (
            (raw["assets"],)
            if isinstance(raw["assets"], str)
            else tuple(dict.fromkeys(raw["assets"]))
        )
        if not assets or any(not isinstance(a, str) or a != a.upper() for a in assets):
            raise ValueError("research bundle assets must be uppercase symbols")
        cutoff = raw["cutoff_utc"]
        try:
            parsed = datetime.fromisoformat(cutoff)
        except (TypeError, ValueError) as exc:
            raise ValueError("cutoff_utc must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("cutoff_utc must be timezone-aware")
        for label in ("evidence", "weights", "adapter_provenance"):
            if not isinstance(raw[label], Mapping) or not raw[label]:
                raise ValueError(f"{label} is required")
        _digest(raw["data_hash"], "data_hash")
        return cls(
            str(raw["run_id"]),
            str(raw["system_id"]),
            str(raw["system_version"]),
            assets,
            str(raw["timeframe"]),
            cutoff,
            raw["data_hash"],
            dict(raw["evidence"]),
            dict(raw["weights"]),
            dict(raw["adapter_provenance"]),
            dict(raw.get("submitted_definition", {})),
            dict(raw.get("effective_definition", {})),
            dict(raw.get("engine_identity", {})),
            dict(raw.get("metrics", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ResearchBundle":
        return cls.from_mapping(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def frozen_payload(self):
        return {
            "run_id": self.run_id,
            "system_id": self.system_id,
            "system_version": self.system_version,
            "assets": list(self.assets),
            "timeframe": self.timeframe,
            "cutoff_utc": self.cutoff_utc,
            "data_hash": self.data_hash,
            "evidence": dict(self.evidence),
            "weights": dict(self.weights),
            "adapter_provenance": dict(self.adapter_provenance),
            "submitted_definition": dict(self.submitted_definition),
            "effective_definition": dict(self.effective_definition),
            "engine_identity": dict(self.engine_identity),
            "metrics": dict(self.metrics),
        }


def validate_research_bundle(
    bundle: ResearchBundle | Mapping[str, Any] | str | Path, *, provider_resolver: Any = None
) -> ResearchBundle:
    bundle = (
        bundle
        if isinstance(bundle, ResearchBundle)
        else (
            ResearchBundle.load(bundle)
            if isinstance(bundle, (str, Path))
            else ResearchBundle.from_mapping(bundle)
        )
    )
    _validate_evidence_reference(bundle.evidence, bundle.run_id)
    # Reject an assessed/unknown inventory before resolving a concrete
    # capability.  This keeps the diagnostic about the governing catalog gate
    # instead of exposing an implementation lookup detail.
    require_adopted_library(provenance_from_mapping(bundle.adapter_provenance))
    if not bundle.engine_identity or not any(
        bundle.engine_identity.get(k) for k in ("engine_sha256", "engine_hash", "version")
    ):
        raise ValueError("engine identity is required")
    if not bundle.metrics or not any(
        bundle.metrics.get(k) is not None
        for k in ("resolved", "trades", "winrate", "net", "holdout")
    ):
        raise ValueError("metrics are required")
    by_asset = bundle.weights.get("by_asset")
    if not isinstance(by_asset, Mapping) or set(by_asset) != set(bundle.assets):
        raise ValueError("weights.by_asset must cover every asset")
    for asset, digest in by_asset.items():
        _digest(digest, f"weights.by_asset.{asset}")
    p = _validate_adapter_provenance(bundle.adapter_provenance)
    validate_runtime_key(p.runtime_key)
    if provider_resolver is not None:
        from ..providers import provider_adoption_gate

        # Resolve against the actual canonical card shape.  Provider health checks
        # are card-aware, so a runtime-key-only probe incorrectly rejects the
        # official provider.
        for asset in bundle.assets:
            card = _make_card(
                bundle,
                asset,
                f"{bundle.system_id}-{asset}",
                f"approved/{asset}/{bundle.system_id}-{asset}.weights",
                by_asset[asset],
                bundle.system_version,
            )
            ok, reason = provider_adoption_gate(
                card, resolver=provider_resolver, runtime_key=p.runtime_key
            )
            if not ok:
                raise ValueError(reason)
    return bundle


def _make_card(bundle, asset, model_id, weights_path, weights_sha256, model_version):
    # Creation time participates in card identity. Bind it to the immutable
    # research cutoff so a retry after a process crash reconstructs the same card.
    p = provenance_from_mapping(bundle.adapter_provenance)
    now = bundle.cutoff_utc
    return ModelCard(
        asset=asset,
        recipe_id=None,
        model_id=model_id,
        status="challenger",
        created_at_utc=now,
        activation_date_utc=None,
        fit_cutoff_utc=bundle.cutoff_utc,
        snapshot_sha256=_sha(bundle.frozen_payload()),
        recipe_sha256=_sha(
            {"system_id": bundle.system_id, "system_version": bundle.system_version}
        ),
        system_id=bundle.system_id,
        system_version=bundle.system_version,
        model_version=model_version,
        timeframe=bundle.timeframe,
        provider=p.runtime_key,
        library_id=p.library_id,
        adapter_key=p.adapter_key,
        adapter_version=p.adapter_version,
        resolved_package=p.resolved_package,
        runtime_key=p.runtime_key,
        library_refs=(p.library_id,),
        evidence_refs=(bundle.run_id,),
        weights_path=weights_path,
        weights_sha256=weights_sha256,
        metrics=dict(bundle.metrics),
        environment={
            "system_id": bundle.system_id,
            "system_version": bundle.system_version,
            "timeframe": bundle.timeframe,
            "runtime_key": p.runtime_key,
            "research_run": bundle.run_id,
            "adapter_key": p.adapter_key,
            "library_id": p.library_id,
            "resolved_package": p.resolved_package,
            "adapter_provenance": p.to_dict(),
            "data_hash": bundle.data_hash,
            "provenance_hash": provenance_hash(p),
        },
    )


def promote_research_bundle(
    bundle,
    registry,
    *,
    weights_by_asset,
    weights_paths=None,
    store=None,
    operation_id=None,
    provider_resolver=None,
    authorized_by=None,
):
    if store is None:
        raise ValueError("promotion requires Store authority")
    if provider_resolver is None:
        raise ValueError("promotion requires provider resolver")
    bundle = validate_research_bundle(bundle, provider_resolver=provider_resolver)
    if not isinstance(authorized_by, str) or not authorized_by.strip():
        raise ValueError("explicit promotion authorization is required")
    if set(bundle.assets) != {"BTCUSDT"}:
        raise ValueError("approved promotion scope is BTCUSDT")
    if set(weights_by_asset) != set(bundle.assets):
        raise ValueError("weights must be provided for every promoted asset")
    bundle_hash = _sha(bundle.frozen_payload())
    txid = operation_id or _sha({"run_id": bundle.run_id})
    stage_root = _staging_dir(registry.root, txid)
    prior = store.promotion_receipt(txid)
    if prior:
        if prior["bundle_hash"] != bundle_hash:
            raise ValueError("promotion receipt identity conflict")
        shutil.rmtree(stage_root, ignore_errors=True)
        body = json.loads(prior["body"])
        if "models" in body:
            return [registry.get(model["id"], model["version"]) for model in body["models"]]
        # A pre-upgrade receipt is tied to its original registered version.
        return [registry.get(mid, store.legacy_model_version(mid) or bundle.system_version)
                for mid in body.get("model_ids", [])]
    model_version = bundle.system_version
    cards = []
    staged = []
    registered = []
    preexisting_paths = set()
    try:
        for asset in bundle.assets:
            raw = weights_by_asset[asset]
            if not isinstance(raw, bytes) or not raw:
                raise ValueError(f"weights missing for {asset}")
            digest = _sha(raw)
            expected = bundle.weights["by_asset"][asset]
            if digest != expected:
                raise ValueError(f"weights hash differs for {asset}")
            card = _make_card(
                bundle,
                asset,
                f"{bundle.system_id}-{asset}",
                (weights_paths or {}).get(
                    asset, registry.versioned_weights_path(asset, f"{bundle.system_id}-{asset}",
                                                           model_version, "model.weights")
                ),
                digest,
                model_version,
            )
            cards.append(card)
            for path in (
                registry._card_path(card),
                registry._state_path(card.model_id, card.model_version),
                Path(registry.root) / card.weights_path,
            ):
                if path.exists():
                    preexisting_paths.add(path)
            stage = stage_root
            stage.mkdir(parents=True, exist_ok=True)
            target = stage / card.weights_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            staged.append((card, target))
        with store.transaction():
            for card, target in staged:
                registry.reconcile(card, target.read_bytes())
                registered.append(card)
                store.upsert_model_metadata(
                    {
                        "model_id": card.model_id,
                        "system_id": card.system_id,
                        "system_version": card.system_version,
                        "model_version": card.model_version,
                        "asset": card.asset,
                        "timeframe": card.timeframe,
                        "status": card.status,
                        "card_hash": card_hash(card),
                        "weights_hash": card.weights_sha256,
                        "provider": card.runtime_key,
                        "library_refs": card.library_refs,
                        "evidence_refs": card.evidence_refs,
                        "provenance": card.provenance.to_dict(),
                    },
                    transition_reason="research_promotion",
                )
            store.register_promotion_receipt(
                txid,
                bundle.run_id,
                bundle_hash,
                {"model_ids": [c.model_id for c in cards],
                 "models": [{"id": c.model_id, "version": c.model_version} for c in cards],
                 "authorized_by": authorized_by.strip()},
            )
        shutil.rmtree(stage_root, ignore_errors=True)
        return cards
    except Exception as exc:
        # Remove only artifacts created by this operation; pre-existing immutable
        # cards/weights remain untouched and the Store transaction is rolled back.
        for card in cards:
            for path in (
                registry._card_path(card),
                registry._state_path(card.model_id, card.model_version),
                Path(registry.root) / card.weights_path,
            ):
                if path in preexisting_paths:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        shutil.rmtree(stage_root, ignore_errors=True)
        store.register_recovery(
            "promotion_failed", {"operation_id": txid, "run_id": bundle.run_id, "error": str(exc)}
        )
        raise


def rollback_model(
    registry,
    asset,
    *,
    to_model_id=None,
    to_model_version=None,
    reason="rollback",
    store=None,
    operation_id=None,
    provider_resolver=None,
):
    if store is None:
        raise ValueError("rollback requires Store authority")
    result = store.rollback_active(
        asset,
        predecessor_model_id=to_model_id,
        predecessor_model_version=to_model_version,
        registry=registry,
        provider_resolver=provider_resolver,
        operation_id=operation_id,
        reason=reason,
    )
    return result["model_id"]


_OPERATION_KEYS = ("environment", "balance", "stops", "sizing", "split", "kill_switch")


def assert_operation_unchanged(before, after):
    for key in _OPERATION_KEYS:
        if before.get(key) != after.get(key):
            raise ValueError(f"promotion changed operational setting: {key}")
    if before.get("environment", "TEST") != "TEST" or after.get("environment", "TEST") != "TEST":
        raise ValueError("promotion is only permitted in TEST environment")
