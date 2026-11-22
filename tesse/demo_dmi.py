"""Verified, first-party BTC M1 DMI rule for the Ebinex demo only."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from ebinex_sdk.services.market import CandleGapError

from . import __version__
from .core import Candle, Params, classify, signals, utcnow
from .models.cards import ModelCard, card_hash

ASSET = "BTCUSDT"
DEMO_PARAMS = Params(3, 3, 10)
SYSTEM_ID = "TREND-DMI-M1-001"
MODEL_ID = f"{SYSTEM_ID}-{ASSET}"
VERSION = "1.0.0"
RUNTIME_KEY = "tesse.demo_dmi_m1@1.0.0"
ADAPTER_KEY = "tesse.dmi_cross@1.0.0"
LIBRARY = "tesse-native"
PACKAGE = f"tesse=={__version__}"
M1 = timedelta(minutes=1)
WARMUP = 64


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def make_demo_artifact(*, evidence_sha256, snapshot_sha256):
    for digest in (evidence_sha256, snapshot_sha256):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("demo artifact requires verified SHA-256 identities")
    value = {
        "format": "tesse.demo-dmi", "schema_version": 1, "environment": "TEST",
        "asset": ASSET, "timeframe": "M1", "lookback": WARMUP, "params": asdict(DEMO_PARAMS),
        "evidence": evidence_sha256, "snapshot": snapshot_sha256,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def validate_demo_provenance(provenance):
    if (provenance.library_id, provenance.adapter_key, provenance.adapter_version,
            provenance.resolved_package, provenance.runtime_key) != (
                LIBRARY, ADAPTER_KEY, VERSION, PACKAGE, RUNTIME_KEY,
            ) or provenance.parameters != asdict(DEMO_PARAMS) or set(provenance.hashes) != {"evidence", "snapshot"}:
        raise ValueError("unrecognized built-in demo DMI provenance")


def signal_on_close(candles, close_at):
    return next((signal for signal in signals(ASSET, candles, DEMO_PARAMS) if signal.candle_at == close_at), None)


class DemoDmiProvider:
    runtime_key = RUNTIME_KEY
    expected_asset = ASSET

    def __init__(self):
        self._loaded = None

    def load(self, card, raw):
        self._loaded = None
        if (card.asset, card.system_id, card.system_version, card.model_version,
                card.timeframe, card.runtime_key, card.environment.get("environment")) != (
                    ASSET, SYSTEM_ID, VERSION, VERSION, "M1", RUNTIME_KEY, "TEST",
                ) or not card.evidence_refs:
            raise ValueError("demo DMI card identity differs")
        prov = card.provenance
        validate_demo_provenance(prov)
        if (_digest(raw) != card.weights_sha256 or card.snapshot_sha256 != prov.hashes["snapshot"]
                or raw != make_demo_artifact(evidence_sha256=prov.hashes["evidence"], snapshot_sha256=prov.hashes["snapshot"])):
            raise ValueError("demo DMI parameter artifact differs from card")
        self._loaded = card_hash(card)
        return self

    def is_healthy(self, card):
        return self._loaded is not None and self._loaded == card_hash(card)

    async def scan(self, client, asset, card, version, params, boundary, *, native_symbol):
        if getattr(getattr(client, "config", None), "environment", None) != "TEST":
            raise ValueError("demo DMI requires a TEST client")
        if asset != ASSET or native_symbol not in {ASSET, "BTCUSDX"}:
            raise ValueError("demo DMI native symbol differs")
        if not self.is_healthy(card):
            return None
        if boundary.utcoffset() != timedelta(0) or boundary.second or boundary.microsecond:
            raise ValueError("demo DMI requires an aligned UTC boundary")
        start, end = boundary - WARMUP * M1, boundary - M1
        try:
            items = await client.market.get_candles(native_symbol, "M1", start=start, end=end, limit=WARMUP, mode="OPTION")
        except CandleGapError as exc:
            items = exc.candles
        candles = [Candle(item.timestamp, float(item.open), float(item.high), float(item.low),
                          float(item.close), float(item.volume) if item.volume is not None else None)
                   for item in sorted(items, key=lambda item: item.timestamp) if start <= item.timestamp <= end]
        signal = signal_on_close(candles, end)
        return replace(signal, model_id=card.model_id, card_sha256=card_hash(card)) if signal else None


def _check_run(run, data_root):
    from .forge.evidence import sha256_file
    from .forge.snapshots import load_snapshot

    raw = run.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("Forge run manifest too large")
    record = yaml.safe_load(raw)
    if (not isinstance(record, dict) or record.get("schema_version") != 2
            or record.get("run_id") != run.parent.name or record.get("status") != "completed"
            or record.get("proposal") != "dmi-rapido-m1" or record.get("experiment") != ASSET.lower()
            or record.get("result_kind") != "comparison" or record.get("training") != {}):
        raise ValueError("expected completed BTC demo DMI Forge comparison")
    experiment = record["effective_experiment"]
    nodes = experiment["nodes"]
    if (nodes.get("rapido") != {"component": "selection.dmi_cross@1.0.0", "params": asdict(DEMO_PARAMS), "inputs": {"candles": "data.candles"}}
            or nodes.get("candidate", {}).get("component") != "replay.option_m1@1.0.0"
            or nodes["candidate"].get("inputs") != {"candles": "data.candles", "proposals": "rapido.proposals"}):
        raise ValueError("Forge candidate does not match the demo rule")
    snapshot = load_snapshot(data_root, experiment["snapshot"])
    if snapshot.asset != ASSET or record["snapshot"]["sha256"] != experiment["snapshot"]["sha256"]:
        raise ValueError("Forge snapshot does not match BTC demo")
    evidence = run.with_name("evidence.sqlite")
    identity = sha256_file(evidence)
    if identity != record["artifact_hashes"].get(evidence.name):
        raise ValueError("Forge evidence hash differs")
    window = experiment["windows"]["evaluation"]
    start, cutoff = (datetime.fromisoformat(window[k]) for k in ("start", "end"))
    created = datetime.fromisoformat(record["created_at"])
    if not (snapshot.span.start <= start < cutoff <= snapshot.span.end and cutoff < created <= utcnow()):
        raise ValueError("Forge evaluation and creation timing differ")
    # Reproduce each persisted candidate against the same OHLC and runtime lookback.
    candles = [Candle(datetime.fromtimestamp(int(t), UTC), *(float(x) for x in (opening, high, low, close, volume)))
               for t, opening, high, low, close, volume in snapshot.bars.tolist()]
    indices = {c.timestamp: i for i, c in enumerate(candles)}
    with closing(sqlite3.connect(evidence.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        rows = db.execute("SELECT asset,anchor_at,decision_at,entry_at,expiry_at,label_at,direction,outcome "
                          "FROM trades WHERE arm='candidate' ORDER BY anchor_at").fetchall()
    if not rows or len(rows) != record["metrics"]["candidate"]["settled"]:
        raise ValueError("Forge candidate settlement count differs")
    total_by_outcome = {"WIN": 0, "LOSS": 0, "TIE": 0}
    for asset, anchor, decision, entry, expiry, label, direction, outcome in rows:
        anchor, decision, entry, expiry, label = (datetime.fromisoformat(x) for x in (anchor, decision, entry, expiry, label))
        index = indices.get(anchor)
        if asset != ASSET or index is None or entry not in indices:
            raise ValueError("Forge candidate candle identity differs")
        signal = signal_on_close(candles[max(0, index - WARMUP + 1):index + 1], anchor)
        candle = candles[indices[entry]]
        observed = "TIE" if candle.open == candle.close else "WIN" if classify(direction, candle) == "WIN" else "LOSS"
        if (signal is None or signal.direction != direction or not start <= decision < cutoff
                or (signal.decision_at, signal.entry_at, signal.expiry_at) != (decision, entry, expiry)
                or label != expiry + timedelta(seconds=2) or observed != outcome):
            raise ValueError("Forge candidate runtime parity differs")
        total_by_outcome[outcome] += 1
    return created, cutoff, _digest(raw), identity, experiment["snapshot"]["sha256"], total_by_outcome


def qualify_demo_dmi(run_path, data_root, registry, store, *, actor):
    if not actor or not actor.strip():
        raise ValueError("demo qualification requires operator approval")
    run = Path(run_path).resolve()
    created, cutoff, run_hash, evidence, snapshot, outcomes = _check_run(run, data_root)
    raw = make_demo_artifact(evidence_sha256=evidence, snapshot_sha256=snapshot)
    model_id = MODEL_ID
    card = ModelCard(
        asset=ASSET, system_id=SYSTEM_ID, system_version=VERSION, model_id=model_id, model_version=VERSION,
        timeframe="M1", runtime_key=RUNTIME_KEY, provider=RUNTIME_KEY, library_id=LIBRARY,
        adapter_key=ADAPTER_KEY, resolved_package=PACKAGE, status="eligible",
        created_at_utc=created.isoformat(), fit_cutoff_utc=cutoff.isoformat(), snapshot_sha256=snapshot,
        recipe_sha256=_digest(ADAPTER_KEY.encode()), weights_path=registry.versioned_weights_path(ASSET, model_id, VERSION, "parameters.json"),
        weights_sha256=_digest(raw), evidence_refs=(run.parent.name,), library_refs=(LIBRARY,),
        metrics={"holdout": outcomes}, environment={"environment": "TEST", "parameters": asdict(DEMO_PARAMS),
            "provenance_hashes": {"evidence": evidence, "snapshot": snapshot}, "forge_manifest_sha256": run_hash},
    )
    DemoDmiProvider().load(card, raw)
    with store.transaction():
        registry.reconcile(card, raw)
        previous = store.get_model_metadata(model_id, VERSION)
        if previous and previous["status"] not in {"eligible", "active"}:
            raise ValueError("demo lifecycle no longer permits qualification")
        metadata = {key: getattr(card, key) for key in ("asset", "model_id", "model_version", "system_id",
            "system_version", "status", "timeframe", "provider", "library_refs", "evidence_refs")}
        metadata.update(card_hash=card_hash(card), weights_hash=card.weights_sha256, provenance=card.provenance.to_dict())
        if previous and previous["status"] == "active":
            if previous["card_hash"] != metadata["card_hash"]:
                raise ValueError("active demo card differs")
        else:
            store.upsert_model_metadata(metadata, transition_reason="demo_dmi_verified_parity")
        store.register_promotion_receipt(_digest(model_id.encode()), run.parent.name, run_hash,
            {"model_ids": [model_id], "authorized_by": actor.strip(), "environment": "TEST", "purpose": "execution_test"})
    return card
