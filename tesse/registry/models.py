"""Persistent model cards and artifacts keyed by exact model identity."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from ..config import atomic_write
from ..models.cards import STATUSES, ModelCard, card_hash, load_card, save_card
from .identity import validate_model_id, validate_version


class ModelArtifactRegistry:
    """Append-only cards; read legacy paths without rewriting their contents."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.cards_root = self.root / "cards"
        self.state_root = self.root / "state"

    def _card_path(self, card: ModelCard) -> Path:
        return self.cards_root / card.asset / card.model_id / f"{card.model_version}.yml"

    def _state_path(self, model_id: str, model_version: str) -> Path:
        return self.state_root / model_id / f"{model_version}.json"

    @staticmethod
    def versioned_weights_path(asset: str, model_id: str, version: str, filename: str) -> str:
        validate_model_id(model_id, asset=asset)
        validate_version(version)
        if not filename or Path(filename).name != filename or filename in {".", ".."}:
            raise ValueError("artifact filename must be a single component")
        return f"approved/{asset}/{model_id}/{version}/{filename}"

    def _matches(self, model_id: str, model_version: str | None = None):
        validate_model_id(model_id)
        asset = model_id.rsplit("-", 1)[1]
        directory = self.cards_root / asset
        paths = [directory / f"{model_id}.yml"]
        if model_version is None:
            paths.extend(sorted((directory / model_id).glob("*.yml")))
        else:
            validate_version(model_version)
            paths.append(directory / model_id / f"{model_version}.yml")
        for path in paths:
            if not path.is_file():
                continue
            card = load_card(path)
            if card.model_id != model_id or card.asset != asset or (
                path.parent != directory and path.stem != card.model_version
            ):
                raise ValueError("model card location differs from identity")
            if model_version is None or card.model_version == model_version:
                yield path, card

    def _find(self, model_id: str, model_version: str | None = None) -> tuple[Path, ModelCard]:
        matches = list(self._matches(model_id, model_version))
        if not matches:
            raise ValueError("unknown model identity")
        if len(matches) > 1:
            raise ValueError("ambiguous model identity; specify an exact, unique version")
        return matches[0]

    def _write_weights(self, card: ModelCard, weights: bytes | None) -> None:
        target = self.root / card.weights_path
        if weights is not None and hashlib.sha256(weights).hexdigest() != card.weights_sha256:
            raise ValueError("weights hash differs from model card")
        if target.exists() or weights is None:
            self.load_weights(card, card.asset)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(prefix=".artifact-", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(weights)
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    self.load_weights(card, card.asset)
            finally:
                temporary.unlink(missing_ok=True)

    def _publish(self, card: ModelCard, weights: bytes | None, *, repair: bool) -> str:
        if not card.is_canonical:
            raise ValueError("legacy card cannot be registered")
        identity = card.model_id, card.model_version
        matches = list(self._matches(*identity))
        digest = card_hash(card)
        if matches:
            if not repair or len(matches) != 1 or card_hash(matches[0][1]) != digest:
                raise ValueError("model card is immutable")
            if matches[0][0] != self._card_path(card):
                # Legacy files can be read and verified, never regenerated or moved.
                self.load_weights(card, card.asset)
                if weights is not None and hashlib.sha256(weights).hexdigest() != card.weights_sha256:
                    raise ValueError("weights hash differs from model card")
                return digest
        else:
            relative = Path(card.weights_path)
            expected = Path(self.versioned_weights_path(card.asset, *identity, relative.name))
            if relative != expected:
                raise ValueError("new artifacts require the exact versioned weights path")
        self._write_weights(card, weights)
        if not matches:
            target = self._card_path(card)
            target.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".card-", dir=target.parent) as staging:
                source = Path(staging) / target.name
                save_card(source, card)
                try:
                    os.link(source, target)
                except FileExistsError as exc:
                    if not repair or card_hash(load_card(target)) != digest:
                        raise ValueError("model card is immutable") from exc
        self._initialize_state(card)
        return digest

    def _initialize_state(self, card: ModelCard) -> None:
        target = self._state_path(card.model_id, card.model_version)
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".state-", dir=target.parent) as staging:
            source = Path(staging) / target.name
            atomic_write(source, self._state_json(card.model_id, card.model_version, card.status, None))
            try:
                os.link(source, target)
            except FileExistsError:
                return

    def register(self, card: ModelCard, weights: bytes | None = None) -> str:
        return self._publish(card, weights, repair=False)

    def reconcile(self, card: ModelCard, weights: bytes) -> str:
        """Repair an interrupted publication without changing card identity."""
        return self._publish(card, weights, repair=True)

    def _write_state(self, model_id: str, model_version: str, status: str, reason: str | None) -> None:
        if status not in STATUSES:
            raise ValueError("unknown model status")
        path = self._state_path(model_id, model_version)
        atomic_write(path, self._state_json(model_id, model_version, status, reason))

    @staticmethod
    def _state_json(model_id: str, model_version: str, status: str, reason: str | None) -> str:
        return json.dumps({
            "model_id": model_id, "model_version": model_version, "status": status, "reason": reason,
        }, sort_keys=True)

    def transition(
        self, model_id: str, status: str, reason: str | None = None, *, model_version: str | None = None
    ) -> None:
        card = self.get(model_id, model_version)
        if status not in STATUSES:
            raise ValueError("unknown model status")
        if status == "active":
            for other in self.list_cards(card.asset, {"active"}):
                if (other.model_id, other.model_version) != (card.model_id, card.model_version):
                    self._write_state(other.model_id, other.model_version, "retired",
                                      f"replaced_by:{model_id}@{card.model_version}")
        self._write_state(model_id, card.model_version, status, reason)

    def get(self, model_id: str, model_version: str | None = None) -> ModelCard:
        return self._find(model_id, model_version)[1]

    def status(self, model_id: str, model_version: str | None = None) -> str:
        card_path, card = self._find(model_id, model_version)
        path = self._state_path(model_id, card.model_version)
        legacy = not path.exists() and card_path != self._card_path(card)
        if legacy:
            path = self.state_root / f"{model_id}.json"
        if not path.exists():
            return card.status
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid model lifecycle state") from exc
        if (not isinstance(raw, dict) or raw.get("model_id") != model_id
                or (not legacy and raw.get("model_version") != card.model_version)
                or raw.get("status") not in STATUSES):
            raise ValueError("invalid model lifecycle state")
        return raw["status"]

    def list_cards(self, asset: str, statuses: set[str] | None = None) -> list[ModelCard]:
        if statuses is not None and not statuses <= STATUSES:
            raise ValueError("unknown model status filter")
        if not isinstance(asset, str) or not asset.isalnum() or not asset.isupper():
            raise ValueError("asset must be an uppercase symbol")
        directory = self.cards_root / asset
        model_ids = {p.stem for p in directory.glob("*.yml")}
        model_ids.update(p.name for p in directory.glob("*") if p.is_dir())
        cards = []
        seen = set()
        for model_id in sorted(model_ids):
            for _, card in self._matches(model_id):
                if (model_id, card.model_version) in seen:
                    raise ValueError("ambiguous model identity in registry")
                seen.add((model_id, card.model_version))
                if statuses is None or self.status(model_id, card.model_version) in statuses:
                    cards.append(card)
        return cards

    def load_weights(self, card: ModelCard, asset: str) -> bytes:
        if card.asset != asset:
            raise ValueError("model card asset does not match requested asset")
        path = self.root / card.weights_path
        if not path.is_file():
            raise ValueError("weights artifact is missing")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != card.weights_sha256:
            raise ValueError("weights hash differs from model card")
        return raw


# Historical import compatibility only; new callers use ModelArtifactRegistry.
ArsenalRegistry = ModelArtifactRegistry
