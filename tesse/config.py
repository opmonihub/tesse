"""YAML estrito, sem segredos e sem construção de objetos Python."""

import hashlib
import json
import os
import warnings
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .core import Params

SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
)
ALIASES = {
    f"{base}{suffix}": f"{base}USDT"
    for base in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB")
    for suffix in ("USDX", "USDT")
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Strategy(Strict):
    dmi_period: int = Field(14, ge=2, le=100, strict=True)
    adx_smoothing: int = Field(14, ge=2, le=100, strict=True)
    min_adx: float = Field(25, ge=0, le=100)

    def params(self):
        return Params(**self.model_dump())


class Risk(Strict):
    session_fraction: float = Field(0.05, gt=0, le=1)


class Limit(Strict):
    value: float = Field(..., gt=0)
    unit: Literal[
        "account_currency",
        "percent_of_session_bank",
        "percent_of_account_balance",
        "percent_of_initial_bank",
    ] = "account_currency"
    scope: Literal["session", "calendar_day"] = "session"


class StopLossSplit(Strict):
    enabled: bool = True
    entries: int = Field(2, ge=1, le=100, strict=True)
    allocation: Literal["equal"] = "equal"
    reserve_before_entry: bool = True


class SorosStaking(Strict):
    enabled: bool = True
    wins_to_complete: int = Field(2, ge=1, le=10, strict=True)
    reinvest_profit: bool = True


class FlatStaking(Strict):
    enabled: bool = True
    amount: float = Field(1, gt=0)
    unit: Literal["account_currency", "percent_of_session_bank"] = "account_currency"


class MartingaleStaking(Strict):
    enabled: bool = False
    multiplier: float = Field(2, ge=1, le=10)
    max_steps: int = Field(2, ge=0, le=10, strict=True)
    reset_on_win: bool = True
    max_session_exposure: float = Field(1, gt=0, le=1)


class Staking(Strict):
    mode: Literal["soros", "flat", "martingale"] = "soros"
    soros: SorosStaking = Field(default_factory=SorosStaking)
    flat: FlatStaking = Field(default_factory=FlatStaking)
    martingale: MartingaleStaking = Field(default_factory=MartingaleStaking)

    @model_validator(mode="after")
    def selected_component_enabled(self):
        selected = getattr(self, self.mode)
        if not selected.enabled:
            raise ValueError(f"Componente de gestão selecionado está desabilitado: {self.mode}")
        return self


class OperationSession(Strict):
    cadence: Literal["daily"] = "daily"
    timezone: str = "UTC"
    start: str = "00:00"
    end: str = "23:59"
    max_sessions_per_day: int = Field(1, ge=1, le=24, strict=True)
    after_stop_win: Literal["next_day", "same_session", "manual_reset"] = "next_day"
    after_stop_loss: Literal["same_session", "next_day", "manual_reset"] = "same_session"

    @model_validator(mode="after")
    def clock_format(self):
        import re

        pattern = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        if not pattern.fullmatch(self.start) or not pattern.fullmatch(self.end):
            raise ValueError("Horários de sessão devem usar HH:MM em UTC ou no timezone declarado")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone da sessão deve ser um identificador IANA válido") from exc
        return self


class BrokerBalance(Strict):
    source: Literal["broker_sdk"] = "broker_sdk"
    refresh: Literal["before_session"] = "before_session"
    override_configured_bank: bool = True


class Operation(Strict):
    profile_id: str = "daily_protected_session"
    environment: Literal["TEST"] = "TEST"
    balance: BrokerBalance = Field(default_factory=BrokerBalance)
    session: OperationSession = Field(default_factory=OperationSession)
    capital: Limit = Field(
        default_factory=lambda: Limit(value=5, unit="percent_of_account_balance", scope="session")
    )
    stop_loss: Limit = Field(default_factory=lambda: Limit(value=10))
    stop_win: Limit = Field(default_factory=lambda: Limit(value=20))
    stop_loss_split: StopLossSplit = Field(default_factory=StopLossSplit)
    staking: Staking = Field(default_factory=Staking)
    max_open_positions: int = Field(1, ge=1, le=1, strict=True)
    reentry: Literal["after_settlement"] = "after_settlement"
    stop_when_limit_reached: bool = True


class Runtime(Strict):
    environment: Literal["TEST"] = "TEST"
    send_margin_seconds: float = Field(10, ge=8, le=30)
    data_directory: str = "data"
    pilot_sessions: Literal[2] = 2
    symbols: Literal["all"] | list[str] = "all"
    aliases: dict[str, str] = Field(default_factory=lambda: dict(ALIASES))

    @model_validator(mode="after")
    def explicit_mapping(self):
        if any(ALIASES.get(k) != v for k, v in self.aliases.items()):
            raise ValueError("Alias fora do inventário explicitamente validado")
        if isinstance(self.symbols, list) and not self.symbols:
            raise ValueError("runtime.symbols deve ser 'all' ou conter pelo menos um ativo")
        return self

    def accepts_symbol(self, symbol: str) -> bool:
        if self.symbols == "all":
            return True
        return symbol in self.symbols or self.aliases.get(symbol) in self.symbols


class History(Strict):
    symbols: list[str] = Field(default_factory=lambda: list(SYMBOLS))
    initial_bank: float = Field(10000, gt=0)
    payout: float = Field(0.9, gt=0, le=1)
    # Canonical configurations write LOSS; LOSE remains readable from broker
    # and historical files during migration.
    tie: Literal["LOSS", "LOSE"] = "LOSS"

    @model_validator(mode="after")
    def symbols_valid(self):
        if tuple(self.symbols) != SYMBOLS:
            raise ValueError(
                "Pesquisa desta versão exige os dez ativos aprovados, na ordem definida"
            )
        return self


class Search(Strict):
    dmi_period: list[int] = Field(default_factory=lambda: [7, 14, 21], min_length=1)
    adx_smoothing: list[int] = Field(default_factory=lambda: [7, 14, 21], min_length=1)
    min_adx: list[float] = Field(default_factory=lambda: [20, 25, 30], min_length=1)

    @model_validator(mode="after")
    def bounded(self):
        if len(self.dmi_period) * len(self.adx_smoothing) * len(self.min_adx) > 100:
            raise ValueError("Máximo 100 candidatas por estudo")
        for n in self.dmi_period:
            for a in self.adx_smoothing:
                for threshold in self.min_adx:
                    Params(n, a, threshold)
        return self


class Gates(Strict):
    minimum_trades: int = Field(200, ge=1)
    minimum_days: int = Field(20, ge=1)
    bootstrap_samples: int = Field(1000, ge=100)
    bootstrap_seed: int = 17
    improvement_pp: float = Field(1, ge=0)


class Learning(Strict):
    enabled: bool = True
    minimum_retention: float = Field(0.2, ge=0.1, le=1)
    minimum_trades: int = Field(200, ge=30)
    minimum_days: int = Field(20, ge=6)


class Config(Strict):
    strategy: Strategy = Field(default_factory=Strategy)
    risk: Risk = Field(default_factory=Risk)
    operation: Operation = Field(default_factory=Operation)
    runtime: Runtime = Field(default_factory=Runtime)
    history: History = Field(default_factory=History)
    search: Search = Field(default_factory=Search)
    gates: Gates = Field(default_factory=Gates)
    learning: Learning = Field(default_factory=Learning)

    def fingerprint(self):
        return hashlib.sha256(json.dumps(serialize_config(self), sort_keys=True).encode()).hexdigest()


def serialize_config(config: Config) -> dict:
    """Serialize canonical configuration without emitting legacy risk fields."""
    data = config.model_dump()
    data.pop("risk", None)
    data["operation"] = effective_operation(config).model_dump()
    return data


def effective_operation(config: Config) -> Operation:
    """Return operation with read-only legacy capital fallback applied."""
    source = getattr(config, "_capital_source", None)
    if not source or source.get("source") != "risk.session_fraction":
        return config.operation
    return config.operation.model_copy(update={"capital": Limit.model_validate(source["capital"])})


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_default(path: Path):
    if path.exists():
        raise ValueError("Configuração já existe; use configure para inspecionar")
    atomic_write(path, yaml.safe_dump(serialize_config(Config()), sort_keys=False, allow_unicode=True))


def load_config(path: Path) -> Config:
    try:
        if path.stat().st_size > 128_000:
            raise ValueError("YAML muito grande")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        runtime = raw.get("runtime") if isinstance(raw, dict) else None
        retired_review = {"auto_review", "daily_review_utc_hour"}
        if isinstance(runtime, dict) and retired_review.intersection(runtime):
            warnings.warn("Opções antigas de revisão automática ignoradas: o worker não agenda revisão",
                          DeprecationWarning, stacklevel=2)
            raw = {**raw, "runtime": {key: value for key, value in runtime.items() if key not in retired_review}}
        config = Config.model_validate(raw)
        if isinstance(raw, dict) and "operation" not in raw and isinstance(raw.get("risk"), dict) and "session_fraction" in raw["risk"]:
            source = resolve_capital_source(raw, config)
            object.__setattr__(config, "_capital_source", source)
        return config
    except (yaml.YAMLError, RecursionError, TypeError) as exc:
        raise ValueError("YAML inválido; não use tags nem objetos") from exc


def params_dict(params):
    if isinstance(params, BaseModel):
        return params.model_dump()
    return asdict(params) if isinstance(params, Params) else dict(params)


def data_path(path: Path, config: Config):
    root = Path(config.runtime.data_directory).expanduser()
    return (root if root.is_absolute() else path.parent / root).resolve()


def resolve_capital_source(raw: object, config: Config | None = None) -> dict:
    """Resolve the canonical stake source without mutating configuration.

    New configurations use ``operation.capital``.  A legacy mapping that has
    only ``risk.session_fraction`` is accepted as a read-only compatibility
    projection and returns deterministic diagnostics so callers can migrate it.
    """
    if config is None:
        if isinstance(raw, Config):
            config = raw
        else:
            config = Config.model_validate(raw)
    mapping = raw if isinstance(raw, dict) else {}
    operation = mapping.get("operation") if isinstance(mapping, dict) else None
    has_capital = isinstance(operation, dict) and "capital" in operation
    risk = mapping.get("risk") if isinstance(mapping, dict) else None
    legacy = risk.get("session_fraction") if isinstance(risk, dict) else None
    if has_capital or legacy is None:
        return {
            "source": "operation.capital",
            "capital": config.operation.capital.model_dump(),
            "diagnostics": [],
        }
    if not isinstance(legacy, (int, float)) or isinstance(legacy, bool) or not 0 < legacy <= 1:
        raise ValueError("risk.session_fraction must be between 0 and 1")
    capital = {
        "value": round(float(legacy) * 100, 12),
        "unit": "percent_of_account_balance",
        "scope": "session",
    }
    return {
        "source": "risk.session_fraction",
        "capital": capital,
        "diagnostics": [
            "legacy risk.session_fraction is read-only; write operation.capital instead",
        ],
    }
