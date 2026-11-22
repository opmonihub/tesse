"""Descriptive diagnostics over a block's verified study evidence.

These measures do not refit, reschedule, alter gates or carry results between
blocks. A trace outcome is official only for an evaluation settlement.
"""

from collections import Counter
from datetime import datetime, timedelta
from fractions import Fraction
from math import fsum, isfinite, log

ARMS = ("control", "candidate")
PHASES = ("calibration", "evaluation")
OUTCOMES = {"WIN": "wins", "LOSS": "losses", "TIE": "ties"}
DAY_SECONDS = 86_400
LOG_LOSS_EPSILON = 1e-15  # Probability formula v1.


def _divide(numerator, denominator):
    return numerator / denominator if denominator else None


def _difference(candidate, control):
    return None if candidate is None or control is None else candidate - control


def _available(reason=None):
    return {"status": "available"} if reason is None else {"status": "unavailable", "reason": reason}


def _daily_window(start, end):
    """Order dates by UTC; durations are intersections with [start, end)."""
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    durations = {}
    while day < end:
        next_day = day + timedelta(days=1)
        durations[day.date().isoformat()] = int((min(next_day, end) - max(day, start)).total_seconds())
        day = next_day
    return durations


def _funnel(rows, admitted_code, seconds):
    """Selection yield uses all opportunities; replay yield uses all proposals."""
    replay = Counter(row["replay"] for row in rows)
    selections = Counter(row["selection"] for row in rows)
    proposed, admitted = len(rows) - replay[None], replay[admitted_code]
    return {
        "duration_seconds": seconds,
        "opportunities": len(rows),
        "proposed": proposed,
        "outside_window": replay["outside_window"],
        "blocked_overlap": replay["blocked_overlap"],
        "admitted": admitted,
        "settled": replay["settled"],
        "proposal_rate": _divide(proposed, len(rows)),
        "admission_rate": _divide(admitted, proposed),
        "coverage_rate": _divide(admitted, len(rows)),
        "opportunities_per_day": _divide(len(rows) * DAY_SECONDS, seconds),
        "proposed_per_day": _divide(proposed * DAY_SECONDS, seconds),
        "admitted_per_day": _divide(admitted * DAY_SECONDS, seconds),
        "selection_counts": dict(sorted(selections.items())),
    }


def _funnel_report(partitions, seconds):
    result = {
        phase: {arm: _funnel(partitions[phase][arm], code, seconds[phase]) for arm in ARMS}
        for phase, code in (("calibration", "admitted"), ("evaluation", "settled"))
    }
    measures = ("opportunities", "proposed", "admitted", "opportunities_per_day", "proposed_per_day",
                "admitted_per_day", "proposal_rate", "admission_rate", "coverage_rate")
    result["evaluation_minus_calibration"] = {
        arm: {key: _difference(result["evaluation"][arm][key], result["calibration"][arm][key])
              for key in measures}
        for arm in ARMS
    }
    return result


def _settlement(counts, seconds):
    """WIN divided by all settlements (including TIE); volume per elapsed 86400s."""
    n = sum(counts[outcome] for outcome in OUTCOMES)
    return {
        "n": n,
        "settled": n,
        **{key: counts[outcome] for outcome, key in OUTCOMES.items()},
        "winrate": _divide(counts["WIN"], n),
        "trades_per_day": _divide(n * DAY_SECONDS, seconds),
    }


def _outcomes(rows):
    return Counter(row["outcome"] for row in rows)


def _concentration(counts_by_day, seconds_by_day, total):
    days = {day: _settlement(counts_by_day[day], seconds) for day, seconds in seconds_by_day.items()}
    active = [day for day, metrics in days.items() if metrics["settled"]]
    top_three = sorted(active, key=lambda day: (-days[day]["settled"], day))[:3]
    return {
        "days": days,
        "active_days": len(active),
        "top_three_days": top_three,
        "top_three_n": sum(days[day]["settled"] for day in top_three),
        "top_three_share": _divide(sum(days[day]["settled"] for day in top_three), total),
        "n": total,
    }


def _delta_winrate(metrics):
    """Keep differences exact until presentation, including ties among dates."""
    a, b = (metrics[arm] for arm in ARMS)
    if a["settled"] and b["settled"]:
        return 100 * (Fraction(b["wins"], b["settled"]) - Fraction(a["wins"], a["settled"]))
    return None


def _leave_one_day_out(totals, counts_by_day, seconds_by_day):
    """Remove one date from both arms; report coverage without rerunning the replay.

    Volume is normalized by the same duration left in the original window, not
    by active days. Retained volume share is relative to that arm's total trades.
    """
    days, defined, undefined = {}, {}, []
    seconds = sum(seconds_by_day.values())
    for day, removed_seconds in seconds_by_day.items():
        remaining_seconds = seconds - removed_seconds
        remaining = {
            arm: _settlement(totals[arm] - counts_by_day[arm][day], remaining_seconds)
            for arm in ARMS
        }
        for arm in ARMS:
            remaining[arm]["retained_volume_share"] = _divide(
                remaining[arm]["settled"], totals[arm].total()
            )
        delta = _delta_winrate(remaining)
        if delta is None:
            undefined.append(day)
        else:
            defined[day] = delta
        days[day] = {
            "remaining_seconds": remaining_seconds,
            "delta_winrate_pp": float(delta) if delta is not None else None,
            **remaining,
        }
    low, high = min(defined.values(), default=None), max(defined.values(), default=None)
    return {
        **_available(None if defined else "no_defined_day_removals"),
        "days": days,
        "min_delta_winrate_pp": float(low) if low is not None else None,
        "max_delta_winrate_pp": float(high) if high is not None else None,
        "min_delta_days": [day for day, delta in defined.items() if delta == low],
        "max_delta_days": [day for day, delta in defined.items() if delta == high],
        "undefined_days": undefined,
    }


def _paired(trades, metrics, totals, counts_by_day, seconds_by_day):
    control, candidate = (
        {(row["asset"], row["anchor_at"]): row for row in trades[arm]} for arm in ARMS
    )
    common = control.keys() & candidate.keys()
    changed = sum(control[key]["direction"] != candidate[key]["direction"] for key in common)
    seconds = sum(seconds_by_day.values())
    delta = _delta_winrate(metrics)
    return {
        "delta_winrate_pp": float(delta) if delta is not None else None,
        "common": {
            "n": len(common),
            "same_direction": len(common) - changed,
            "changed_direction": changed,
            **{arm: _settlement(_outcomes(rows[key] for key in common), seconds)
               for arm, rows in zip(ARMS, (control, candidate), strict=True)},
        },
        "exclusive": {
            arm: _settlement(_outcomes(rows[key] for key in rows.keys() - common), seconds)
            for arm, rows in zip(ARMS, (control, candidate), strict=True)
        },
        "leave_one_day_out": _leave_one_day_out(totals, counts_by_day, seconds_by_day),
    }


def _log_loss(p, y):
    # Clip the probability of the observed outcome, including an exact upper
    # bound, without losing precision in the lower bound to 1 - (1 - epsilon).
    confidence = p if y else 1 - p
    return -log(min(1 - LOG_LOSS_EPSILON, max(LOG_LOSS_EPSILON, confidence)))


def _forecast_stats(rows, end, prior, prior_reason):
    """All rates/scores share the intersection of forecasts and mature labels.

    The larger population is described separately. It is not the denominator
    of the observed frequency, score, or constant-prior comparison.
    """
    eligible = [row for row in rows if row["outcome"] in OUTCOMES
                and datetime.fromisoformat(row["label_at"]) <= end]
    forecasts = [(row["probability_win"], int(row["outcome"] == "WIN"), row["replay"] == "settled")
                 for row in eligible if row["probability_win"] is not None]
    n = len(forecasts)
    wins = sum(y for _, y, _ in forecasts)
    reason = None
    if not rows:
        reason = "empty_population"
    elif not any(row["probability_win"] is not None for row in rows):
        reason = "no_probabilities"
    elif not n:
        reason = "no_mature_forecasts"
    return {
        **_available(reason),
        "population_n": len(rows),
        "labeled_n": len(eligible),
        "unlabeled_n": len(rows) - len(eligible),
        "unscored_labeled_n": len(eligible) - n,
        "n": n,
        "hypothetical_n": sum(not settled for _, _, settled in forecasts),
        "wins": wins,
        "mean_probability_win": _divide(fsum(p for p, _, _ in forecasts), n),
        "observed_winrate": _divide(wins, n),
        "prior_unavailable_reason": prior_reason or reason,
        "brier": {
            "model": _divide(fsum((p - y) ** 2 for p, y, _ in forecasts), n),
            "prior": _divide(wins * (prior - 1) ** 2 + (n - wins) * prior**2, n)
            if prior is not None else None,
        },
        "log_loss": {
            "model": _divide(fsum(_log_loss(p, y) for p, y, _ in forecasts), n),
            "prior": _divide(wins * _log_loss(prior, 1) + (n - wins) * _log_loss(prior, 0), n)
            if prior is not None else None,
        },
    }


def _probability_report(partitions, ends, training_prior):
    samples, wins = training_prior["samples"], training_prior["wins"]
    prior = _divide(wins, samples)
    report = {
        "formula_version": 1,
        "log_loss_epsilon": LOG_LOSS_EPSILON,
        "training_prior": {"samples": samples, "wins": wins, "probability_win": prior},
    }
    for phase, accepted in (("calibration", "admitted"), ("evaluation", "settled")):
        report[phase] = {}
        for arm, rows in partitions[phase].items():
            populations = {
                "mature": rows,
                "proposals": [row for row in rows if row["replay"] is not None],
                "accepted": [row for row in rows if row["replay"] == accepted],
            }
            arm_prior = prior if arm == "candidate" else None
            prior_reason = None if arm_prior is not None else (
                "no_training_support" if arm == "candidate" else "no_arm_training_prior"
            )
            report[phase][arm] = {
                key: _forecast_stats(population, ends[phase], arm_prior, prior_reason)
                for key, population in populations.items()
            }
    return report


def _payoff_report(totals, scenario):
    """Assumed net return per settled unit, not broker payout or a gate metric."""
    if scenario is None or not all(key in scenario and isfinite(scenario[key]) for key in OUTCOMES):
        return _available("no_complete_scenario")
    amounts = {key: Fraction(str(scenario[key])) for key in OUTCOMES}
    result = {**_available(), "scenario": {key: scenario[key] for key in OUTCOMES}}
    for arm, counts in totals.items():
        n = counts.total()
        net = sum(amounts[key] * counts[key] for key in OUTCOMES)
        result[arm] = {
            **_available(None if n else "no_settlements"),
            "n": n,
            "net_return_per_unit": float(net / n) if n else None,
        }
    return result


def block_diagnostics(block, opportunities, training_prior, payoff=None):
    """Return stable check IDs for one block from validated windows and trace rows.

    Windows and trace timestamps use canonical UTC ISO strings. ``training_prior``
    gives the candidate's actual fit support (``samples`` and ``wins``), never a
    calibration or evaluation rate. Neither raw directional scores nor hypothetical
    outcomes qualify as official trades. Unavailable rates are ``None``.
    """
    partitions = {phase: {arm: [] for arm in ARMS} for phase in PHASES}
    for row in opportunities:
        partitions[row["phase"]][row["arm"]].append(row)
    windows = {phase: tuple(datetime.fromisoformat(block[phase][key]) for key in ("start", "end"))
               for phase in PHASES}
    ends = {phase: end for phase, (_, end) in windows.items()}
    seconds = {phase: int((end - start).total_seconds()) for phase, (start, end) in windows.items()}
    seconds_by_day = _daily_window(*windows["evaluation"])
    trades = {arm: [row for row in rows if row["replay"] == "settled"]
              for arm, rows in partitions["evaluation"].items()}
    totals = {arm: _outcomes(rows) for arm, rows in trades.items()}
    counts_by_day = {arm: {day: Counter() for day in seconds_by_day} for arm in ARMS}
    for arm, rows in trades.items():
        for row in rows:
            counts_by_day[arm][row["entry_at"][:10]][row["outcome"]] += 1
    settlement = {arm: _settlement(totals[arm], seconds["evaluation"]) for arm in ARMS}
    return {
        "funnel": _funnel_report(partitions, seconds),
        "settlement": settlement,
        "concentration": {arm: _concentration(counts_by_day[arm], seconds_by_day, settlement[arm]["n"])
                          for arm in ARMS},
        "paired": _paired(trades, settlement, totals, counts_by_day, seconds_by_day),
        "probability": _probability_report(partitions, ends, training_prior),
        "payoff": _payoff_report(totals, payoff),
    }
