import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import yaml

from tesse import core, demo_dmi
from tesse.config import Config
from tesse.core import Candle, Params, Signal, classify, classify_canonical
from tesse.data import DTYPE
from tesse.execution.shadow import ShadowLedger
from tesse.forge import components, snapshots
from tesse.forge.components import Frame, Proposals
from tesse.forge.contracts import Window
from tesse.forge.snapshots import Snapshot
from tesse.models.replay import simulate
from tesse.state import Store

ASSET = "BTCUSDT"
ANCHOR = datetime(2026, 1, 2, tzinfo=UTC)
CASES = pytest.mark.parametrize(
    ("direction", "side", "close", "outcome", "legacy", "label"),
    [
        ("CALL", 1, 101.0, "WIN", "WIN", 1),
        ("CALL", 1, 99.0, "LOSS", "LOSE", 0),
        ("CALL", 1, 100.0, "TIE", "LOSE", 0),
        ("PUT", -1, 101.0, "LOSS", "LOSE", 0),
        ("PUT", -1, 99.0, "WIN", "WIN", 1),
        ("PUT", -1, 100.0, "TIE", "LOSE", 0),
    ],
)


def _candles(close):
    return [
        Candle(ANCHOR + timedelta(minutes=i), 100.0, 102.0, 98.0, price, 0.0)
        for i, price in enumerate((100.0, 100.0, close, 100.0))
    ]


def _snapshot(candles):
    bars = np.array(
        [(int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close, c.volume) for c in candles],
        dtype=DTYPE,
    )
    return Snapshot(bars, ASSET, "fixture", Window(ANCHOR, ANCHOR + timedelta(minutes=4)))


@pytest.mark.parametrize(("close", "movement"), [(101.0, 1), (99.0, -1), (100.0, 0)])
def test_price_movement_is_a_direction_not_a_settlement(close, movement):
    assert core.price_movement(100.0, close) == movement


@CASES
def test_price_based_projections_remain_compatible(
    direction, side, close, outcome, legacy, label, tmp_path
):
    candles = _candles(close)
    entry = candles[2]
    signal = Signal(ASSET, ANCHOR, direction, 30, 8)
    assert classify(direction, entry) == legacy
    assert classify_canonical(direction, entry) == ("WIN" if legacy == "WIN" else "LOSS")
    replay = simulate(ASSET, candles, Params(), candidates=[signal], staking="flat")
    assert [trade["status"] for trade in replay["trades"]] == [legacy]

    snapshot = _snapshot(candles)
    anchor = int(ANCHOR.timestamp())
    proposals = Proposals(np.array([anchor]), np.array([side]))
    book = components.replay(snapshot, proposals, snapshot.span)
    assert [trade["outcome"] for trade in book.trades] == [outcome]

    # The second example has no entry candle; the original sentinel is not a binary loss.
    anchors = np.array([anchor, anchor + 120])
    frame = Frame(anchors, np.array([side, side]), np.ones(2), np.ones((2, 1)))
    assert components.label_direction(snapshot.bars, frame).y.tolist() == [label, -1]

    with Store(tmp_path / "shadow.sqlite") as store:
        ledger = ShadowLedger(Config(), store)
        pending = ledger.record_signal(signal, version="fixture", params=Params())
        assert pending["status"] == "PENDING"
        settled = ledger.settle_due({ASSET: {signal.entry_at: entry}}, now=signal.expiry_at)
        assert [trade["status"] for trade in settled] == [outcome]


@pytest.mark.parametrize(
    ("direction", "close", "outcome"),
    [
        ("CALL", 101.0, "WIN"), ("CALL", 99.0, "LOSS"), ("CALL", 100.0, "TIE"),
        ("PUT", 101.0, "LOSS"), ("PUT", 99.0, "WIN"), ("PUT", 100.0, "TIE"),
    ],
)
def test_offline_demo_checks_existing_forge_outcome(
    direction, close, outcome, tmp_path, monkeypatch
):
    signal = Signal(ASSET, ANCHOR, direction, 30, 8)
    snapshot = _snapshot(_candles(close))
    snapshot_id = "a" * 64
    run_dir = tmp_path / "fixture-run"
    run_dir.mkdir()
    evidence = run_dir / "evidence.sqlite"
    with closing(sqlite3.connect(evidence)) as db, db:
        db.execute("""CREATE TABLE trades (
            arm, asset, anchor_at, decision_at, entry_at, expiry_at, label_at, direction, outcome
        )""")
        timestamps = (
            signal.candle_at, signal.decision_at, signal.entry_at, signal.expiry_at,
            signal.expiry_at + timedelta(seconds=2),
        )
        db.execute("INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            "candidate", ASSET, *(t.isoformat() for t in timestamps), direction, outcome,
        ))
    run = run_dir / "run.yaml"
    run.write_text(yaml.safe_dump({
        "schema_version": 2, "run_id": run_dir.name, "status": "completed",
        "proposal": "dmi-rapido-m1", "experiment": ASSET.lower(),
        "result_kind": "comparison", "training": {},
        "snapshot": {"sha256": snapshot_id},
        "artifact_hashes": {evidence.name: hashlib.sha256(evidence.read_bytes()).hexdigest()},
        "created_at": (snapshot.span.end + timedelta(minutes=1)).isoformat(),
        "metrics": {"candidate": {"settled": 1}},
        "effective_experiment": {
            "nodes": {
                "rapido": {
                    "component": "selection.dmi_cross@1.0.0",
                    "params": {"dmi_period": 3, "adx_smoothing": 3, "min_adx": 10},
                    "inputs": {"candles": "data.candles"},
                },
                "candidate": {
                    "component": "replay.option_m1@1.0.0",
                    "inputs": {"candles": "data.candles", "proposals": "rapido.proposals"},
                },
            },
            "snapshot": {"sha256": snapshot_id},
            "windows": {"evaluation": snapshot.span.as_dict()},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(snapshots, "load_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(demo_dmi, "signal_on_close", lambda *_: signal)

    *_, totals = demo_dmi._check_run(run, tmp_path)
    assert totals == {name: int(name == outcome) for name in ("WIN", "LOSS", "TIE")}
