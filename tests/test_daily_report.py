import json
from pathlib import Path

import pytest

from tesse.cli import daily_report
from tesse.state import Store

DAY = "2026-01-02"
PRIMARY_CONTEXT = "BTCUSDT CALL ADX 25-30"


@pytest.fixture
def store(tmp_path):
    with Store(Path(":memory:"), model_root=tmp_path / "models") as instance:
        yield instance


def _intent(store, state, *, entry=f"{DAY}T00:00:00Z", symbol="BTCUSDT", side="CALL", adx=27):
    store.db.execute(
        """INSERT INTO intents
        (id, session_id, symbol, direction, candle_at, entry_at, stake, state, context)
        VALUES (?, 'fixture', ?, ?, ?, ?, '1', ?, ?)""",
        (state, symbol, side, entry, entry, state, json.dumps({"adx": adx})),
    )


def test_daily_report_normalizes_aliases_and_keeps_refunded_separate(store):
    for state in ("LOSS", "LOSE", "TIE", "REFUNDED"):
        _intent(store, state)
    before = store.db.execute("SELECT * FROM intents ORDER BY id").fetchall()

    report = daily_report(store, DAY)

    assert report["outcomes"] == {"LOSS": 2, "TIE": 1, "REFUNDED": 1}
    assert report["contexts"] == {PRIMARY_CONTEXT: {"LOSS": 2, "TIE": 1}}
    assert store.db.execute("SELECT * FROM intents ORDER BY id").fetchall() == before


@pytest.mark.parametrize("context", [{}, {"adx": None}])
def test_daily_report_keeps_tie_without_adx_only_in_total(store, context):
    _intent(store, "TIE")
    stored = json.dumps(context)
    store.db.execute("UPDATE intents SET context = ? WHERE id = 'TIE'", (stored,))
    _intent(store, "WIN", adx=0)

    report = daily_report(store, DAY)

    assert report["outcomes"] == {"TIE": 1, "WIN": 1}
    assert report["contexts"] == {"BTCUSDT CALL ADX 0-5": {"WIN": 1}}
    assert store.db.execute("SELECT context FROM intents WHERE id = 'TIE'").fetchone()[0] == stored


def test_daily_report_keeps_day_context_and_technical_boundaries(store):
    _intent(store, "lOsE")
    _intent(store, "win", entry=f"{DAY}T23:59:00Z", symbol="ETHUSDT", side="PUT", adx=30)
    _intent(store, "tIe", side="PUT")
    _intent(store, "UNKNOWN")
    _intent(store, "historical-custom")
    _intent(store, "WIN", entry="2026-01-01T23:59:00Z")
    _intent(store, "LOSS", entry="2026-01-03T00:00:00Z")
    store.db.executemany("INSERT INTO events (at, category, body) VALUES (?, ?, '{}')", [
        (f"{DAY}T00:00:00Z", "data_gap"),
        (f"{DAY}T00:00:00Z", "shadow_result"),
        (f"{DAY}T00:00:00Z", "strategy_result"),
        ("2026-01-03T00:00:00Z", "data_gap"),
    ])

    report = daily_report(store, DAY)

    assert report["day"] == DAY
    assert report["outcomes"] == {
        "LOSS": 1, "WIN": 1, "TIE": 1, "UNKNOWN": 1, "historical-custom": 1,
    }
    assert report["contexts"] == {
        PRIMARY_CONTEXT: {"LOSS": 1},
        "BTCUSDT PUT ADX 25-30": {"TIE": 1},
        "ETHUSDT PUT ADX 30-35": {"WIN": 1},
    }
    assert report["technical_and_data_events"] == {"data_gap": 1}
