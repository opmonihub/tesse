"""Read the diagnosis for exactly one verified run without opening candles."""

from .contracts import Invalid
from .diagnostics import block_diagnostics
from .evidence import EVIDENCE_NAME
from .guard import workspace_layout_guard
from .layout import project_run_directory
from .review import open_verified_run
from .study_evidence import read_block

DIAGNOSIS_VERSION = 1


def report_from_evidence(receipt, db):
    report = {
        "schema_version": DIAGNOSIS_VERSION,
        **{key: receipt[key] for key in ("proposal", "experiment", "run_id", "verdict")},
        "evidence_sha256": receipt["artifact_hashes"][EVIDENCE_NAME],
    }
    if "study" not in receipt:
        return report | {"status": "unavailable", "reason": (
            "confirmation_run" if receipt.get("phase") == "confirmation" else "legacy_run"
        )}
    study = receipt["study"]
    declaration = study["declaration"]
    blocks = []
    for block in declaration["blocks"]:
        result, opportunities = read_block(db, block["id"])
        fit = result.pop("training")
        fits = list(fit.values()) if isinstance(fit, dict) else []
        if (len(fits) != 1 or not isinstance(fits[0], dict)
                or type(fits[0].get("samples")) is not int or type(fits[0].get("wins")) is not int
                or not 0 <= fits[0]["wins"] <= fits[0]["samples"]):
            raise Invalid("evidence.sqlite.study", f"suporte de treino inválido: {block['id']}")
        try:
            metrics = block_diagnostics(block, opportunities, fits[0], declaration.get("payoff"))
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise Invalid("evidence.sqlite.study", f"oportunidade incompatível: {block['id']}") from exc
        fitted = fits[0]
        training = {name: fitted.get(name) for name in ("adapter", "resolved_version", "last_label_at", "samples", "wins")}
        training["cutoff"] = block["train"]["end"]
        training["winrate"] = fitted["wins"] / fitted["samples"] if fitted["samples"] else None
        blocks.append({"block": block, **result, "training": training, "diagnostics": metrics})
    return report | {"status": "available", "round_id": study["round_id"],
                     "declaration_hash": study["declaration_hash"],
                     "declaration": declaration, "snapshot": receipt["snapshot"],
                     "criteria": receipt["effective_experiment"]["criteria"], "blocks": blocks}


def diagnose_run(folder, *, expected_proposal=None, expected_run=None):
    with open_verified_run(folder, expected_proposal=expected_proposal, expected_run=expected_run) as (receipt, db):
        return report_from_evidence(receipt, db)


def diagnose_project_run(root, proposal, run_id):
    with workspace_layout_guard(root):
        return diagnose_run(project_run_directory(root, proposal, run_id),
                            expected_proposal=proposal, expected_run=run_id)


def format_report(report):
    heading = f"{report['proposal']} / {report['experiment']} / {report['run_id']}"
    if report["status"] != "available":
        return f"{heading}: diagnóstico indisponível ({report['reason']})."
    lines = [f"{heading}: {report['verdict']}", report["declaration"]["question"]]
    for block in report["blocks"]:
        diagnostics = block["diagnostics"]
        bounds = block["block"]["evaluation"]
        lines.append(f"{block['block']['id']} [{bounds['start']}, {bounds['end']}): {block['verdict']}")
        fit = block["training"]
        rate = fit["winrate"]
        training_rate = f"{rate:.2%}" if rate is not None else "indisponível"
        adapter = fit['adapter'] or 'adaptador não registrado'
        version = fit['resolved_version'] or 'versão não registrada'
        lines.append(f"  Treino: {fit['samples']} linhas, {training_rate} WIN; {adapter} ({version}); "
                     f"último rótulo: {fit['last_label_at'] or 'não registrado'}; cutoff: {fit['cutoff']}")
        for arm in ("control", "candidate"):
            settled = diagnostics["settlement"][arm]
            rate = settled["winrate"]
            winrate = f"{rate:.2%}" if rate is not None else "indisponível"
            days = diagnostics["concentration"][arm]["active_days"]
            lines.append(f"  {arm}: {settled['wins']}/{settled['n']} WIN ({winrate}), "
                         f"{settled['trades_per_day']:.2f}/dia, {days} dia(s) ativo(s)")
            for phase in ("calibration", "evaluation"):
                funnel = diagnostics["funnel"][phase][arm]
                lines.append(f"    {phase}: {funnel['opportunities']} pontuadas, "
                             f"{funnel['proposed']} propostas, {funnel['admitted']} admitidas")
        paired = diagnostics["paired"]["leave_one_day_out"]
        if paired["status"] == "available":
            lines.append(f"  Retirada do mesmo dia: {paired['min_delta_winrate_pp']:+.2f} a "
                         f"{paired['max_delta_winrate_pp']:+.2f} pp (não é intervalo de confiança)")
        prob = diagnostics["probability"]["evaluation"]["candidate"]["mature"]
        if prob["status"] == "available" and prob["brier"]["prior"] is not None:
            lines.append(f"  Brier candidato/prior de treino: {prob['brier']['model']:.4f}/"
                         f"{prob['brier']['prior']:.4f} (n={prob['n']}, mesmas âncoras)")
        if diagnostics["payoff"]["status"] == "available":
            lines.append("  Payoff disponível no JSON: simulação, não resultado de corretora.")
    return "\n".join(lines)
