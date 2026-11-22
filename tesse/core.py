"""Núcleo determinístico compartilhado entre replay e operação."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from .indicators import DMI, Indicator
from .indicators.base import valid_volume
from .outcomes import normalize_outcome


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    def __post_init__(self):
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("Candle exige timestamp UTC")
        if self.timestamp.second or self.timestamp.microsecond:
            raise ValueError("Candle M1 desalinhado")
        values = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Candle não finito")
        if min(values) <= 0 or (self.volume is not None and not valid_volume(self.volume)):
            raise ValueError("Preço/volume inválido")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC inconsistente")


@dataclass(frozen=True)
class Params:
    dmi_period: int = 14
    adx_smoothing: int = 14
    min_adx: float = 25

    def __post_init__(self):
        if not 2 <= self.dmi_period <= 100 or not 2 <= self.adx_smoothing <= 100:
            raise ValueError("Períodos entre 2 e 100")
        if not math.isfinite(self.min_adx) or not 0 <= self.min_adx <= 100:
            raise ValueError("ADX entre 0 e 100")


def crossed(prev: Indicator | None, current: Indicator | None, threshold: float) -> str | None:
    if prev is None or current is None or current.adx < threshold:
        return None
    if prev.plus <= prev.minus and current.plus > current.minus:
        return "CALL"
    if prev.plus >= prev.minus and current.plus < current.minus:
        return "PUT"
    return None


@dataclass(frozen=True)
class Signal:
    symbol: str
    candle_at: datetime
    direction: str
    adx: float
    distance: float
    model_id: str | None = None
    recipe_id: str | None = None
    card_sha256: str | None = None
    confidence: float | None = None

    def __post_init__(self):
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("Signal confidence must be finite and between 0 and 1")

    @property
    def decision_at(self):
        return self.candle_at + timedelta(seconds=62)

    @property
    def entry_at(self):
        return self.candle_at + timedelta(minutes=2)

    @property
    def expiry_at(self):
        return self.entry_at + timedelta(minutes=1)

    def sendable(self, now: datetime, margin: float = 10):
        return self.decision_at <= now < self.entry_at - timedelta(seconds=margin)


def signals(symbol: str, candles, params: Params):
    dmi, previous = DMI(params), None
    for candle in candles:
        current = dmi.push(candle)
        direction = crossed(previous, current, params.min_adx)
        if direction:
            yield Signal(
                symbol, candle.timestamp, direction, current.adx, abs(current.plus - current.minus)
            )
        previous = current


def rank_signals(items):
    def key(signal):
        # Model confidence is primary when a provider supplies it. Signals
        # without confidence remain deterministic DMI fallbacks for research.
        confidence = signal.confidence if signal.confidence is not None else 0.0
        return (-confidence, -signal.adx, -signal.distance, signal.symbol)

    return sorted(items, key=key)


def price_movement(opening: float, closing: float) -> int:
    """Compare finite prices: +1 up, -1 down, 0 unchanged. Not a settlement."""
    if closing > opening:
        return 1
    if closing < opening:
        return -1
    return 0


def classify(direction: str, candle: Candle) -> str:
    """Project the legacy replay result, including LOSE for a flat candle."""
    if direction not in {"CALL", "PUT"}:
        raise ValueError("Direção inválida")
    win = price_movement(candle.open, candle.close) == (1 if direction == "CALL" else -1)
    return "WIN" if win else "LOSE"


def classify_canonical(direction: str, candle: Candle) -> str:
    """Return the legacy price projection in canonical spelling, not broker status."""
    return normalize_outcome(classify(direction, candle))


def floor_money(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)




class SessionUnavailable(ValueError):
    """A sessão operacional não pode aceitar uma nova entrada agora."""


def operational_day(entry_at: datetime, timezone: str, start: str, end: str) -> str:
    if entry_at.utcoffset() is None:
        raise SessionUnavailable("Entrada exige horário com timezone")
    local = entry_at.astimezone(ZoneInfo(timezone))
    minute = local.strftime("%H:%M")
    inside = start <= minute <= end if start <= end else minute >= start or minute <= end
    if not inside:
        raise SessionUnavailable("Entrada fora da janela da sessão operacional")
    return local.date().isoformat()


@dataclass
class Session:
    initial_bank: Decimal
    stake: Decimal
    version: str
    wins: int = 0
    complete: bool = False
    bank: Decimal | None = None
    net: Decimal = Decimal(0)
    losses: Decimal = Decimal(0)
    entries_used: int = 0
    staking_mode: str = "soros"
    soros_wins_to_complete: int = 2
    flat_stake: Decimal | None = None
    stop_loss: Decimal | None = None
    stop_win: Decimal | None = None
    stop_loss_scope: str = "session"
    stop_win_scope: str = "session"
    max_entries: int | None = None
    max_sessions_per_day: int = 1
    session_day: str | None = None
    balance_source: str = "legacy"
    close_reason: str | None = None
    after_stop_win: str = "next_day"
    after_stop_loss: str = "same_session"

    session_timezone: str = "UTC"
    session_start: str = "00:00"
    session_end: str = "23:59"
    stop_when_limit_reached: bool = True
    reinvest_profit: bool = True
    reserve_split_before_entry: bool = True
    original_stop_loss: Decimal | None = None
    original_stop_win: Decimal | None = None
    starting_day_net: Decimal = Decimal(0)
    operation_fingerprint: str | None = None
    demo_continuous: bool = False  # Historical sessions only; retained for settlement on recovery.

    def __post_init__(self):
        if self.bank is None:
            self.bank = self.initial_bank
        if self.flat_stake is None and self.staking_mode == "flat":
            self.flat_stake = self.stake

    @classmethod
    def begin(cls, bank: Decimal, version: str, fraction=Decimal("0.05")):
        stake = floor_money(bank * fraction)
        if stake < 1:
            raise ValueError("Banca insuficiente: entrada inteira menor que 1")
        return cls(bank, stake, version)

    @staticmethod
    def _limit_value(limit, bank: Decimal) -> Decimal:
        value = Decimal(str(limit.value))
        if limit.unit in {"percent_of_account_balance", "percent_of_initial_bank", "percent_of_session_bank"}:
            value = bank * value / Decimal(100)
        return value

    @classmethod
    def begin_operational(cls, bank: Decimal, version: str, operation, session_day: str):
        bank = Decimal(bank)
        if not bank.is_finite() or bank <= 0:
            raise ValueError("Saldo da corretora inválido")
        mode = operation.staking.mode
        if mode == "martingale":
            raise ValueError("Martingale ainda não está implementado no executor")
        capital = cls._limit_value(operation.capital, bank)
        if mode == "flat":
            flat = operation.staking.flat
            stake = Decimal(str(flat.amount))
            if flat.unit == "percent_of_session_bank":
                stake = bank * stake / Decimal(100)
            flat_stake = stake
        else:
            stake = capital
            flat_stake = None
        stop_loss = cls._limit_value(operation.stop_loss, bank)
        stop_win = cls._limit_value(operation.stop_win, bank)
        split = operation.stop_loss_split
        max_entries = split.entries if split.enabled else 1
        reserve_split = split.enabled and split.reserve_before_entry
        stake = floor_money(min(stake, bank, stop_loss / (max_entries if reserve_split else 1)))
        if stake < 1:
            raise ValueError("Banca insuficiente: entrada operacional menor que 1")
        if flat_stake is not None:
            flat_stake = stake
        soros_wins = operation.staking.soros.wins_to_complete if mode == "soros" else 0
        return cls(
            bank,
            stake,
            version,
            staking_mode=mode,
            soros_wins_to_complete=soros_wins,
            flat_stake=flat_stake,
            stop_loss=stop_loss,
            stop_win=stop_win,
            stop_loss_scope=operation.stop_loss.scope,
            stop_win_scope=operation.stop_win.scope,
            max_entries=max_entries,
            max_sessions_per_day=operation.session.max_sessions_per_day,
            session_day=session_day,
            balance_source=operation.balance.source,
            after_stop_win=operation.session.after_stop_win,
            after_stop_loss=operation.session.after_stop_loss,
            session_timezone=operation.session.timezone,
            session_start=operation.session.start,
            session_end=operation.session.end,
            stop_when_limit_reached=operation.stop_when_limit_reached,
            reinvest_profit=operation.staking.soros.reinvest_profit,
            reserve_split_before_entry=reserve_split,
            original_stop_loss=stop_loss,
            original_stop_win=stop_win,
        )

    def bounded_stake(self) -> Decimal:
        if self.complete:
            return Decimal(0)
        if self.staking_mode == "flat":
            candidate = self.flat_stake or self.stake
        else:
            candidate = self.stake
        if self.stop_loss is not None:
            remaining = self.stop_loss - self.losses
            entries_left = max((self.max_entries or 1) - self.entries_used, 1)
            reserved = entries_left if self.reserve_split_before_entry else 1
            candidate = min(candidate, self.bank, remaining / Decimal(reserved))
        candidate = floor_money(candidate)
        return max(candidate, Decimal(0))

    def resolve(self, status: str, profit: Decimal) -> Decimal:
        status = normalize_outcome(status, source="broker")
        if self.complete or status not in {"WIN", "LOSS", "TIE", "REFUNDED"}:
            raise ValueError("Resultado não resolvido ou sessão encerrada")
        if not profit.is_finite() or profit < 0:
            raise ValueError("Lucro confirmado inválido")
        if status == "REFUNDED":
            return Decimal(0)
        self.entries_used += 1
        if status == "TIE":
            net = Decimal(0)
        elif status == "LOSS":
            net = -self.stake
            self.losses += self.stake
        else:
            net = profit
            self.wins += 1
        self.net += net
        self.bank = (self.bank or self.initial_bank) + net
        if self.balance_source == "legacy" and status == "LOSS":
            self.complete = True
            self.close_reason = "legacy_loss"
        elif (
            self.stop_loss is not None
            and self.losses >= self.stop_loss
        ):
            self.complete, self.close_reason = True, "stop_loss"
        elif (
            self.stop_when_limit_reached and self.stop_win is not None
            and self.starting_day_net + self.net >= self.stop_win
        ):
            self.complete, self.close_reason = True, "stop_win"
        elif self.max_entries is not None and self.entries_used >= self.max_entries:
            self.complete, self.close_reason = True, "max_entries"
        elif self.staking_mode == "soros" and self.wins >= self.soros_wins_to_complete:
            self.complete, self.close_reason = True, "staking_cycle"
        if not self.complete:
            if self.staking_mode == "soros" and status == "WIN" and self.reinvest_profit:
                self.stake = floor_money(self.stake + profit)
            self.stake = self.bounded_stake()
            if self.stake < 1:
                self.complete = True
                self.close_reason = "capital_floor"
        return net


def utcnow():
    return datetime.now(UTC)
