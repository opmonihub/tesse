"""Shared settlement replay used by the active model and evidence paths."""

import csv
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from ..config import atomic_write
from ..core import Session, classify, signals
from ..streaks import trade_streak_metrics


def simulate(
    symbol,
    candles,
    params,
    *,
    initial_bank=Decimal(10000),
    payout=Decimal("0.9"),
    fraction=Decimal("0.05"),
    candidates=None,
    start=None,
    end=None,
    version="research",
    staking="soros",
    candle_index=None,
):
    if staking not in {"soros", "flat"}:
        raise ValueError("staking deve ser soros ou flat")
    bank = Decimal(initial_bank)
    if candle_index is None:
        lookup = {c.timestamp: c for c in candles}
    else:
        if candle_index.rows is not candles:
            raise ValueError("Candle index belongs to different candles")
        lookup = candle_index
    candidates = signals(symbol, candles, params) if candidates is None else candidates
    session = None
    free_at = None
    peak, max_dd = bank, 0.0
    trades, completed, wins, missing, sessions_ww = [], 0, 0, 0, 0
    bankrupt = False
    ineligible = []

    def exclude(signal, reason):
        ineligible.append(dict(candle_at=signal.candle_at.isoformat(),
                               direction=signal.direction, reason=reason))

    for signal in candidates:
        if (start and signal.decision_at < start) or (end and signal.expiry_at > end):
            exclude(signal, "outside_scope")
            continue
        if free_at and signal.decision_at < free_at:
            exclude(signal, "overlap")
            continue
        entry = lookup.get(signal.entry_at)
        if entry is None or signal.candle_at + timedelta(minutes=1) not in lookup:
            missing += 1
            exclude(signal, "missing_candle")
            continue
        if staking == "soros" and session is None:
            try:
                session = Session.begin(bank, version, fraction)
            except ValueError:
                bankrupt = True
                exclude(signal, "insufficient_capital")
                continue
        stake = session.stake if staking == "soros" else Decimal(1)
        status = classify(signal.direction, entry)
        profit = stake * payout if status == "WIN" else Decimal(0)
        net = session.resolve(status, profit) if staking == "soros" else (
            profit if status == "WIN" else -stake
        )
        bank += net
        if staking == "flat" and bank < 1:
            bankrupt = True  # Diagnostic only: keep the unit series uncensored.
        peak = max(peak, bank)
        max_dd = max(max_dd, float((peak - bank) / peak))
        wins += status == "WIN"
        trades.append(
            dict(
                symbol=symbol,
                signal_at=signal.candle_at.isoformat(),
                decision_at=signal.decision_at.isoformat(),
                entry_at=signal.entry_at.isoformat(),
                expiry_at=signal.expiry_at.isoformat(),
                direction=signal.direction,
                adx=signal.adx,
                distance=signal.distance,
                status=status,
                stake=str(stake),
                net=str(net),
                bank=str(bank),
                day=signal.entry_at.date().isoformat(),
            )
        )
        free_at = signal.expiry_at
        if session is not None and session.complete:
            completed += 1
            sessions_ww += session.wins == 2
            session = None
    return dict(
        symbol=symbol,
        resolved=len(trades),
        wins=wins,
        winrate=wins / len(trades) if trades else 0,
        active_days=len({t["day"] for t in trades}),
        initial_bank=str(initial_bank),
        final_bank=str(bank),
        net=str(bank - initial_bank),
        drawdown=max_dd,
        completed_sessions=completed,
        two_win_sessions=sessions_ww,
        incomplete_sessions=int(session is not None),
        missing_entry=missing,
        bank_too_small=bankrupt,
        staking=staking,
        capital_unconstrained=staking == "flat",
        trades=trades,
        ineligible=ineligible,
        **trade_streak_metrics(trades),
    )


def write_report(directory: Path, result, label):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (label + ".json")
    atomic_write(path, json.dumps(result, indent=2, ensure_ascii=False, default=str))
    trades = result.get("trades", [])
    if trades:
        with path.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as out:
            writer = csv.DictWriter(out, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
    return path
