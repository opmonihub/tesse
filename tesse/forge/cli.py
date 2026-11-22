"""CLI dispatch deliberately independent of bot configuration and SDK."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from .contracts import DEFAULT_WORKSPACE_ROOT, Invalid
from .layout import projects_directory
from .validation import (
    SCHEMA_VERSION,
    Issue,
    Validation,
    load_workspace,
    validate_proposal,
)


def register(commands):
    forge = commands.add_parser("forge", help="Teste e refinamento offline de estratégias")
    actions = forge.add_subparsers(dest="forge_action", required=True)
    validate = actions.add_parser("validate", help="Validar os YAMLs sem abrir os dados")
    target = validate.add_mutually_exclusive_group(required=True)
    target.add_argument("proposal", nargs="?", metavar="PROPOSTA")
    target.add_argument("--all", action="store_true", help="Validar todas as propostas ativas")
    validate.add_argument("--study", type=Path, help="Validar declaração diagnóstica sem abrir os dados")
    validate.add_argument("--strict", action="store_true", help="Reprovar avisos")
    validate.add_argument("--json", action="store_true", help="Emitir relatório JSON")
    run = actions.add_parser("run", help="Executar um experimento com candles locais")
    run.add_argument("proposal", metavar="PROPOSTA")
    run.add_argument("--experiment", metavar="ID", required=True)
    run.add_argument("--study", type=Path, help="Declaração versionada de estudo gerenciado")
    run.add_argument("--export-research-model", action="store_true", help="Exportar JSON nativo do XGBoost avulso")
    run.add_argument("--data-root", type=Path, help="Raiz dos snapshots; sobrepõe data_root do workspace")
    iterate = actions.add_parser("iterate", help="Iterar comparações sob um plano congelado")
    iterate.add_argument("proposal", metavar="PROPOSTA")
    iterate.add_argument("--experiment", metavar="ID")
    iterate.add_argument("--plan", type=Path, help="Plano v2 de iteração a congelar")
    iterate.add_argument("--iteration", metavar="ID", help="ID de uma iteração existente")
    iterate.add_argument("--state-revision", help="Rejeitar ações baseadas em uma revisão de estado obsoleta")
    iterate.add_argument("--diagnose", action="store_true", help="Diagnosticar o run de desenvolvimento da iteração")
    iterate.add_argument("--retry-study", type=Path, help="Repetir falha comprovada com novo ID e mesma declaração")
    iterate.add_argument("--submit-review", type=Path, help="Registrar revisão explícita da rodada")
    iterate.add_argument("--resume", action="store_true", help="Retomar a próxima etapa elegível")
    iterate.add_argument("--approve-adjustment", metavar="JSON", help="Aprovar o ajuste JSON exato pendente")
    iterate.add_argument("--freeze-candidate", nargs="?", const="true", metavar="RUN_ID", help="Congelar um candidato de desenvolvimento")
    iterate.add_argument("--approve-confirmation", action="store_true", help="Autorizar a abertura final")
    iterate.add_argument("--confirm", action="store_true", help="Executar a confirmação final autorizada")
    iterate.add_argument("--data-root", type=Path, help="Raiz dos snapshots; sobrepõe data_root do workspace")
    iterate.add_argument("--json", action="store_true", help="Emitir relatório JSON")
    diagnose = actions.add_parser("diagnose", help="Diagnosticar um run exato sem reexecutar")
    review = actions.add_parser("review", help="Verificar um run exato ou registrar sua revisão")
    for action in (diagnose, review):
        action.add_argument("proposal", metavar="PROPOSTA")
        action.add_argument("--run", metavar="ID", required=True)
        action.add_argument("--json", action="store_true")
    review.add_argument("--record", type=Path, help="Registrar revisão de estudo avulso")
    migration = actions.add_parser("migrate-projects", help="Migrar proposals/ para projects/ sem reexecutar")
    for action in (validate, run, iterate, diagnose, review, migration):
        action.add_argument(
            "--root", type=Path, default=DEFAULT_WORKSPACE_ROOT,
            help="Raiz do workspace Forge (padrão: %(default)s/)",
        )


def _json(payload):
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2))


def _active(root):
    active = projects_directory(root)
    if not active.exists():
        return []
    return sorted(p.name for p in active.iterdir()
                  if not p.name.startswith(".") and (p.is_symlink() or p.is_dir()))


def _report(args):
    study = None
    try:
        if args.all and args.study is not None:
            raise Invalid("study", "--study exige uma proposta exata, não --all")
        workspace = load_workspace(args.root)
        ids = _active(workspace.root) if args.all else [args.proposal]
        if not ids:
            raise Invalid("root", "nenhuma proposta ativa")
        results = [(i, validate_proposal(workspace, i)) for i in ids]
        if args.study is not None and results[0][1].valid():
            from .study import load_study

            study = load_study(args.study, results[0][1].definition)
    except Invalid as exc:
        results = [(args.proposal, Validation(None, (Issue("ERROR", exc.field, exc.message),)))]
    except OSError as exc:
        issue = Issue("ERROR", "root", f"raiz do workspace indisponível: {exc}")
        results = [(args.proposal, Validation(None, (issue,)))]
    items = [dict(id=i, type="proposal", valid=r.valid(strict=args.strict),
                  issues=[asdict(issue) for issue in r.issues]) for i, r in results]
    if study is not None:
        items[0].update(study=study.as_dict(), study_hash=study.digest)
    passed = sum(item["valid"] for item in items)
    totals = {"items": len(items), "passed": passed, "failed": len(items) - passed}
    if args.json:
        _json({"items": items, "summary": {"totals": totals, "byType": {"proposal": totals}},
               "version": SCHEMA_VERSION, "root": {"path": str(args.root.resolve())}})
    else:
        for item in items:
            status = "válido" if item["valid"] else "inválido"
            print(f"{item['id'] or args.root}: {status}")
            for issue in item["issues"]:
                print(f"  [{issue['level']}] {issue['path']}: {issue['message']}")
        if study is not None:
            print(f"  estudo {study.round_id}: {len(study.blocks)} bloco(s), SHA-256 {study.digest}")
        if args.all:
            print(f"{passed}/{len(items)} válidos")
    return int(bool(totals["failed"]))


def dispatch(args):
    if args.forge_action == "migrate-projects":
        from .migration import migrate_projects

        try:
            moved = migrate_projects(args.root)
        except (OSError, ValueError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        print("proposals/ migrado para projects/." if moved else "workspace já usa projects/; sem alterações.")
        return 0
    if args.forge_action == "validate":
        return _report(args)
    if args.forge_action == "iterate":
        from .iteration import cli_iterate
        return cli_iterate(args)
    if args.forge_action in {"diagnose", "review"}:
        return _inspect_run(args)
    from .runner import run

    try:
        receipt = run(args.root, args.proposal, args.experiment, data_root=args.data_root, study_path=args.study,
                      export_research_model=args.export_research_model)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    _json(receipt)
    return int(receipt.get("research_model", {}).get("status") == "error")


def _inspect_run(args):
    from .diagnose import diagnose_project_run, format_report
    from .review import review_project_run

    try:
        if args.forge_action == "diagnose":
            result = diagnose_project_run(args.root, args.proposal, args.run)
            if not args.json:
                print(format_report(result))
                return 0
        elif args.record is not None:
            from .managed import record_standalone_review

            result = record_standalone_review(args.root, args.proposal, args.run, args.record)
        else:
            result = review_project_run(args.root, args.proposal, args.run)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    _json(result)
    return 0
