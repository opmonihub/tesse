"""Aprendiz supervisionado de regras pequenas, simétricas e auditáveis.

WIN/LOSE são rótulos para treino. O filtro recebe somente features disponíveis
no fechamento da vela do sinal. Nenhum identificador de ativo vira uma feature.
"""

import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import timedelta
from statistics import median

import numpy as np

from .core import DMI, Signal, classify, crossed

FEATURES = (
    "adx",
    "adx_delta",
    "di_distance",
    "body_ratio",
    "aligned_body",
    "close_location",
    "range_atr",
    "move_3_atr",
)


@dataclass(frozen=True)
class Term:
    feature: str
    operator: str
    threshold: float

    def __post_init__(self):
        if self.feature not in FEATURES or self.operator not in {">=", "<="}:
            raise ValueError("Feature/operador fora do contrato causal de entrada")
        if not math.isfinite(self.threshold):
            raise ValueError("Limiar não finito")

    def matches(self, values):
        value = values.get(self.feature)
        if value is None or not math.isfinite(value):
            return False
        return value >= self.threshold if self.operator == ">=" else value <= self.threshold


@dataclass(frozen=True)
class Rule:
    terms: tuple[Term, ...] = ()

    def __post_init__(self):
        if len(self.terms) > 2:
            raise ValueError("Regra limitada a duas condições")

    def matches(self, features):
        return all(term.matches(features) for term in self.terms)

    def to_dict(self):
        return {"all": [asdict(term) for term in self.terms]}

    @classmethod
    def from_dict(cls, body):
        if not isinstance(body, dict) or set(body) != {"all"}:
            raise ValueError("Regra inválida")
        return cls(tuple(Term(**t) for t in body["all"]))


@dataclass(frozen=True)
class Observation:
    signal: Signal
    features: dict[str, float]
    status: str

    @property
    def day(self):
        return self.signal.entry_at.date().isoformat()

    def to_dict(self):
        return {
            **asdict(self.signal),
            "signal_at": self.signal.candle_at.isoformat(),
            "decision_at": self.signal.decision_at.isoformat(),
            "entry_at": self.signal.entry_at.isoformat(),
            "expiry_at": self.signal.expiry_at.isoformat(),
            "features": self.features,
            "status": self.status,
            "day": self.day,
        }


def observations(symbol, candles, params, *, start=None, end=None):
    """Censo de sinais, sem truncamento por banca. Resultado sempre da vela +2."""
    dmi, previous = DMI(params), None
    history = deque(maxlen=4)
    result = []
    for index, candle in enumerate(candles):
        if history and candle.timestamp != history[-1].timestamp + timedelta(minutes=1):
            history.clear()
            previous = None
        history.append(candle)
        current = dmi.push(candle)
        direction = crossed(previous, current, params.min_adx)
        if direction and len(history) == 4:
            signal = Signal(
                symbol, candle.timestamp, direction, current.adx, abs(current.plus - current.minus)
            )
            if (
                (start is None or signal.decision_at >= start)
                and (end is None or signal.expiry_at <= end)
                and index + 2 < len(candles)
                and candles[index + 1].timestamp == candle.timestamp + timedelta(minutes=1)
                and candles[index + 2].timestamp == signal.entry_at
            ):
                span = candle.high - candle.low
                sign = 1 if direction == "CALL" else -1
                atr = dmi.tr
                features = dict(
                    adx=current.adx,
                    adx_delta=current.adx - previous.adx,
                    di_distance=signal.distance,
                    body_ratio=abs(candle.close - candle.open) / span if span else 0,
                    aligned_body=sign * (candle.close - candle.open) / span if span else 0,
                    close_location=sign * (2 * candle.close - candle.high - candle.low) / span
                    if span
                    else 0,
                    range_atr=span / atr if atr else 0,
                    move_3_atr=sign * (candle.close - history[0].close) / atr if atr else 0,
                )
                result.append(
                    Observation(signal, features, classify(direction, candles[index + 2]))
                )
        previous = current
    return result


def stats(rows, payout=0.9):
    wins = sum(o.status == "WIN" for o in rows)
    count = len(rows)
    return dict(
        resolved=count,
        wins=wins,
        losses=count - wins,
        winrate=wins / count if count else None,
        active_days=len({o.day for o in rows}),
        net_per_unit=(payout * wins - (count - wins)) / count if count else None,
    )


def compare(rows, rule, payout=0.9):
    kept, removed = [], []
    for observation in rows:
        (kept if rule.matches(observation.features) else removed).append(observation)
    return dict(
        baseline=stats(rows, payout),
        retained=stats(kept, payout),
        retention=len(kept) / len(rows) if rows else 0,
        removed_losses=sum(o.status == "LOSE" for o in removed),
        removed_wins=sum(o.status == "WIN" for o in removed),
    )


def summarize(rows, payout=0.9):
    groups = {
        name: defaultdict(list)
        for name in ("by_symbol", "by_direction", "by_hour", "by_adx", "by_previous_result")
    }
    pending, known = defaultdict(deque), {}
    for o in sorted(rows, key=lambda o: (o.signal.candle_at, o.signal.symbol)):
        groups["by_symbol"][o.signal.symbol].append(o)
        groups["by_direction"][o.signal.direction].append(o)
        groups["by_hour"][f"{o.signal.entry_at.hour:02}"].append(o)
        low = int(o.features.get("adx", o.signal.adx) // 5) * 5
        groups["by_adx"][f"{low:02}-{low + 5:02}"].append(o)
        # Only a result already expired at this decision is an observable previous result.
        queue = pending[o.signal.symbol]
        while queue and queue[0].signal.expiry_at <= o.signal.decision_at:
            known[o.signal.symbol] = queue.popleft().status
        category = known.get(o.signal.symbol, "NONE")
        groups["by_previous_result"][category].append(o)
        queue.append(o)
    return dict(
        overall_signal_counts=stats(rows, payout),
        **{
            name: {k: stats(v, payout) for k, v in sorted(values.items())}
            for name, values in groups.items()
        },
    )


def fit_rules(
    rows, *, minimum_trades=200, minimum_days=20, minimum_retention=0.2, expected_symbols=None
):
    """Limiar aprende no prefixo; seleção em dois blocos posteriores, sem shuffle.

    Este é treino com seleção interna, não inferência estatística sobre o futuro.
    O objetivo usa a menor winrate por ativo/bloco, não a média que esconde ativos ruins.
    """
    rows = sorted(rows, key=lambda o: (o.signal.candle_at, o.signal.symbol))
    details = dict(
        status="insufficient_evidence", candidates_tested=0, algorithm="bounded_rule_learning_v1"
    )
    if not rows:
        return Rule(), details
    first = rows[0].signal.decision_at.replace(hour=0, minute=0, second=0, microsecond=0)
    end = rows[-1].signal.expiry_at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    span_days = (end - first).days
    cut = first + timedelta(days=int(span_days * 0.6))
    second = cut + timedelta(days=int((end - cut).days * 0.5))
    fit = [o for o in rows if o.signal.expiry_at <= cut]
    check = [
        [o for o in rows if start <= o.signal.decision_at and o.signal.expiry_at <= stop]
        for start, stop in ((cut, second), (second, end))
    ]
    details.update(
        threshold_fit_end=cut.isoformat(),
        temporal_selection_end=end.isoformat(),
        temporal_blocks=[
            (cut.isoformat(), second.isoformat()),
            (second.isoformat(), end.isoformat()),
        ],
    )
    symbols = sorted({o.signal.symbol for o in rows})
    missing = sorted(set(expected_symbols or symbols) - set(symbols))
    details["missing_symbols"] = missing
    if missing:
        return Rule(), details
    if span_days < 6 or len(symbols) < 2:
        return Rule(), details
    for symbol in symbols:
        support = stats([o for o in rows if o.signal.symbol == symbol])
        if support["resolved"] < minimum_trades or support["active_days"] < minimum_days:
            return Rule(), details

    phase_trades = max(10, minimum_trades // 3)
    phase_days = max(2, minimum_days // 3)

    def arrays(part):
        return (
            np.array([o.status == "WIN" for o in part]),
            {s: np.array([o.signal.symbol == s for o in part]) for s in symbols},
            np.array([o.day for o in part]),
            part,
        )

    fit_array = arrays(fit)
    check_arrays = [arrays(part) for part in check]

    def score(rule, group):
        labels, by_symbol, days, part = group
        mask = np.array([rule.matches(o.features) for o in part], dtype=bool)
        rates, retentions = [], []
        for symbol, scope in by_symbol.items():
            selected = mask & scope
            n, base = int(selected.sum()), int(scope.sum())
            if (
                n < phase_trades
                or not base
                or n / base < minimum_retention
                or len(np.unique(days[selected])) < phase_days
            ):
                return None
            rates.append(float(labels[selected].mean()))
            retentions.append(n / base)
        return min(rates) - 0.001 * len(rule.terms), median(rates), min(retentions)

    if score(Rule(), fit_array) is None or any(score(Rule(), g) is None for g in check_arrays):
        return Rule(), details
    terms = []
    for feature in FEATURES:
        if not all(feature in o.features for o in fit):
            continue
        for threshold in sorted(
            set(
                float(q)
                for q in np.quantile([o.features[feature] for o in fit], [0.2, 0.4, 0.6, 0.8])
            )
        ):
            terms.extend(Term(feature, operator, threshold) for operator in (">=", "<="))
    candidates = [Rule()] + [Rule((term,)) for term in terms]
    scores = {r: s for r in candidates if (s := score(r, fit_array)) is not None}
    attempted = set(candidates)
    beam = sorted(scores, key=lambda r: scores[r], reverse=True)[:8]
    for base in beam:
        for term in terms:
            if base.terms and all(t.feature != term.feature for t in base.terms):
                rule = Rule(
                    tuple(
                        sorted(
                            (*base.terms, term), key=lambda t: (t.feature, t.operator, t.threshold)
                        )
                    )
                )
                if rule not in attempted:
                    attempted.add(rule)
                    if (s := score(rule, fit_array)) is not None:
                        scores[rule] = s
    # Only training chooses five finalists; validation labels never create thresholds/conditions.
    finalists = sorted(scores, key=lambda r: scores[r], reverse=True)[:5]
    finalists = list(dict.fromkeys([Rule(), *finalists]))
    ranks = {}
    for rule in finalists:
        values = [score(rule, group) for group in check_arrays]
        if all(value is not None for value in values):
            ranks[rule] = (
                min(v[0] for v in values),
                median(v[1] for v in values),
                -len(rule.terms),
            )
    details.update(
        candidates_tested=len(attempted),
        finalists=len(finalists),
        train_rows=len(fit),
        selection_rows=sum(map(len, check)),
    )
    baseline = ranks[Rule()]
    best = max(ranks, key=lambda r: ranks[r])
    if ranks[best][0] < baseline[0] + 0.005:
        best = Rule()
    details.update(
        status="learned" if best.terms else "no_stable_improvement",
        baseline_worst_block_winrate=baseline[0],
        selected_worst_block_score=ranks[best][0],
        rule=best.to_dict(),
        fit_score=scores[best],
        selection_score=ranks[best],
    )
    return best, details
