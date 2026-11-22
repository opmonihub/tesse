"""The catalog is descriptive; only a consistent operational snapshot reports lifecycle."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from ..config import data_path, load_config
from ..models.cards import STATUSES
from .catalog import CatalogRegistry
from .identity import ModelIdentity

_STATES = ("active", "eligible", "challenger", "retired", "rejected")


def _read_snapshot(config_path):
    source = data_path(config_path, load_config(config_path)) / "state.sqlite"
    # Do not honor a TMPDIR inside the installation. The parent owns the
    # scratch directory so it is cleaned even if the snapshot process times out.
    locations = (Path("/tmp"), Path("/var/tmp")) if os.name != "nt" else (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Temp",
        Path(os.environ.get("SystemRoot", "C:/Windows")) / "Temp",
    )
    scratch = next((p for p in (location.resolve() for location in locations)
                    if p.is_dir() and os.access(p, os.W_OK)
                    and not any(p.is_relative_to(root) for root in (config_path.parent, source.parent))), None)
    if scratch is None:
        raise ValueError("sem diretório temporário privado fora da instalação")
    with TemporaryDirectory(prefix="tesse-inventory-", dir=scratch) as temp:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "tesse.registry.inventory_snapshot", str(source),
             str(Path(temp) / "snapshot.sqlite")],
            check=True, capture_output=True, text=True, timeout=8,
        )
        rows = json.loads(completed.stdout)
    if "error" in rows:
        raise ValueError(rows["error"])
    return rows


def _model_key(row):
    return row["asset"], row["model_id"], row["model_version"]


def _resolve_operational(rows, catalog):
    states = {status: [] for status in _STATES}
    registered, unresolved, models, active = set(), [], {}, set()
    for row in rows["models"]:
        definition = (row["system_id"], row["system_version"])
        try:
            ModelIdentity(**{k: v for k, v in row.items() if k != "status"})
            if definition not in catalog:
                raise ValueError("versão exata do System ausente no catálogo")
            if catalog[definition].metadata.get("timeframe", row["timeframe"]) != row["timeframe"]:
                raise ValueError("timeframe não corresponde ao System")
            if row["status"] not in STATUSES:
                raise ValueError("estado operacional desconhecido")
        except (TypeError, ValueError) as exc:
            unresolved.append({"kind": "model", **row, "reason": str(exc)})
            continue
        registered.add(definition)
        models[_model_key(row)] = row
    for pointer in rows["pointers"]:
        key = _model_key(pointer)
        model = models.get(key)
        if model is None or model["status"] != "active":
            unresolved.append({"kind": "active_pointer", **pointer,
                               "reason": "ponteiro não corresponde a um modelo ativo de identidade exata"})
        else:
            active.add(key)
    for key, row in models.items():
        if row["status"] == "active" and key not in active:
            unresolved.append({"kind": "model", **row, "reason": "modelo ativo sem ponteiro correspondente"})
        else:
            states[row["status"]].append(row)
    return {"available": True, "states": states, "unresolved": unresolved}, registered


def strategy_inventory(config_path):
    definitions = {(r.identity, r.version): r for r in CatalogRegistry().records(kind="system")}
    try:
        operational, registered = _resolve_operational(_read_snapshot(config_path.resolve()), definitions)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        operational, registered = {"available": False, "reason": str(exc)}, set()
    catalog = [
        {"system_id": identity[0], "system_version": identity[1], "status": record.status,
         "definition": record.metadata["definition"], "provenance": record.provenance,
         "registration": ("unknown" if not operational["available"] else
                          "registered" if identity in registered else "catalog_only")}
        for identity, record in definitions.items()
    ]
    return {"catalog": catalog, "operational": operational}


def render_inventory(result):
    print("Catálogo de estratégias (estado declarativo)")
    for row in result["catalog"]:
        print(f"  {row['system_id']}@{row['system_version']} — {row['status']} / {row['registration']}")
    operational = result["operational"]
    if not operational["available"]:
        print(f"\nEstado operacional indisponível: {operational['reason']}")
        return
    print("\nEstado operacional (Store)")
    for status, rows in operational["states"].items():
        print(f"  {status} ({len(rows)})")
        for row in rows:
            print(f"    {row['asset']} {row['model_id']}@{row['model_version']} "
                  f"[{row['system_id']}@{row['system_version']}]")
    print(f"  Referências não resolvidas ({len(operational['unresolved'])})")
    for row in operational["unresolved"]:
        print(f"    {row['kind']}: {row['asset']} {row['model_id']}@{row['model_version']} — {row['reason']}")
