"""Isolated virtual accounting. No broker account balances, orders, or intents."""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from ..config import effective_operation
from ..core import (
    Params,
    Session,
    SessionUnavailable,
    classify_canonical,
    operational_day,
    price_movement,
)
from ..state import dumps

ASSETS = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"})
PARAMS = Params(3, 3, 10)
VERSION = "shadow-dmi-cross-3-3-10-v1"


class ShadowLedger:
    def __init__(self, config, store, initial_bank=None):
        self.config, self.store = config, store
        self.operation = effective_operation(config)
        self.fingerprint = dumps(self.operation.model_dump())
        seed = Decimal(str(config.history.initial_bank if initial_bank is None else initial_bank))
        if not seed.is_finite() or seed <= 0:
            raise ValueError("invalid initial virtual bank")
        with store.transaction():
            if store.get("shadow_ledger") is None:
                store.set("shadow_ledger", {"initial_bank": seed, "virtual_bank": seed})

    def _session_for(self, state, signal, version):
        window = self.operation.session
        try:
            day = operational_day(signal.entry_at, window.timezone, window.start, window.end)
        except SessionUnavailable:
            return None, "outside_session_window"
        lock = state.get("lock")
        if lock and (lock["policy"] == "manual_reset" or lock["day"] == day):
            return None, "virtual_" + lock["reason"]
        previous_day = state.get("day")
        if previous_day and previous_day > day:
            return None, "stale_session_day"
        if previous_day != day:
            state.update(day=day, day_net="0", day_losses="0", sessions=0, session=None)
        if state.get("session"):
            session = self.store.restore({"body": dumps(state["session"])})
            if not session.complete:
                if session.operation_fingerprint != self.fingerprint or session.version != version:
                    return None, "virtual_session_profile_changed"
                return session, None
        if state["sessions"] >= window.max_sessions_per_day:
            return None, "max_sessions_per_day"
        try:
            session = Session.begin_operational(Decimal(state["virtual_bank"]), version, self.operation, day)
        except ValueError:
            return None, "virtual_staking_unavailable"
        session.operation_fingerprint = self.fingerprint
        if session.stop_loss_scope == "calendar_day":
            remaining = session.stop_loss - Decimal(state["day_losses"])
            if remaining < 1:
                return None, "virtual_stop_loss"
            session.stop_loss = remaining
            session.max_entries = min(session.max_entries, int(remaining))
        if session.stop_win_scope == "calendar_day":
            session.starting_day_net = Decimal(state["day_net"])
            if session.stop_when_limit_reached and session.starting_day_net >= session.stop_win:
                return None, "virtual_stop_win"
        session.stake = session.bounded_stake()
        if session.stake < 1:
            return None, "virtual_capital_floor"
        state["sessions"] += 1
        return session, None

    def record_signal(self, signal, *, version, params, now=None, reason=None, native_symbol=None):
        store = self.store
        with store.transaction():
            known = store.shadow_signal(signal)
            if known:
                return known
            state = store.get("shadow_ledger")
            session = None
            if reason is None and now is not None and not signal.sendable(now, self.config.runtime.send_margin_seconds):
                reason = "late_signal"
            if reason is None and store.shadow_trades(status="PENDING"):
                reason = "virtual_overlap"
            if reason is None:
                session, reason = self._session_for(state, signal, version)
            row = store.record_shadow_signal(
                signal, stake=session.stake if session else 0, version=version, params=params, reason=reason,
            )
            if session is not None:
                state.update(session=asdict(session), pending_id=row["id"],
                             pending_native_symbol=native_symbol, payout=str(self.config.history.payout))
                store.set("shadow_ledger", state)
            return row

    def pending_native_symbol(self, trade):
        state = self.store.get("shadow_ledger") or {}
        return state.get("pending_native_symbol") if state.get("pending_id") == trade["id"] else None

    def settle_due(self, candles, *, now):
        """A missing asset means data unavailable; an included asset may prove a gap."""
        store = self.store
        with store.transaction():
            results = []
            for row in store.shadow_trades(status="PENDING"):
                if now < datetime.fromisoformat(row["expiry_at"]) or row["asset"] not in candles:
                    continue
                state = store.get("shadow_ledger")
                if state.get("pending_id") != row["id"]:
                    raise ValueError("virtual ledger pending identity mismatch")
                entry_at = datetime.fromisoformat(row["entry_at"])
                candle = candles[row["asset"]].get(entry_at)
                if candle is None:
                    status, net, reason = "MISSING_CANDLE", Decimal(0), "missing_entry_candle"
                else:
                    if candle.timestamp != entry_at:
                        raise ValueError("virtual settlement entry candle mismatch")
                    status = (
                        "TIE" if price_movement(candle.open, candle.close) == 0
                        else classify_canonical(row["direction"], candle)
                    )
                    session = store.restore({"body": dumps(state["session"])})
                    if session.stake != Decimal(row["stake"]):
                        raise ValueError("virtual settlement stake mismatch")
                    profit = session.stake * Decimal(state["payout"]) if status == "WIN" else Decimal(0)
                    net, reason = session.resolve(status, profit), None
                    day_losses = Decimal(state["day_losses"]) + (-net if status == "LOSS" else 0)
                    day_net = Decimal(state["day_net"]) + net
                    state.update(virtual_bank=session.bank, day_losses=day_losses, day_net=day_net, session=asdict(session))
                    if session.complete:
                        close = session.close_reason
                        if session.stop_loss_scope == "calendar_day" and session.original_stop_loss - day_losses < 1:
                            close = "stop_loss"
                        policy = {"stop_loss": session.after_stop_loss, "stop_win": session.after_stop_win}.get(close)
                        if policy in {"next_day", "manual_reset"}:
                            state["lock"] = {"day": state["day"], "policy": policy, "reason": close}
                results.append(store.settle_shadow_trade(row["id"], status=status, net=net, settled_at=now, reason=reason))
                state.update(pending_id=None, pending_native_symbol=None, payout=None)
                store.set("shadow_ledger", state)
            return results

    def report(self):
        return self.store.shadow_report()
