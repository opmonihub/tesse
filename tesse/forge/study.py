"""Strict data-only declarations for managed Forge development rounds."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from .contracts import Invalid, Window, keys, slug, text
from .validation import parse_windows, parse_yaml, read_limited, require_version

STUDY_VERSION = 1
DIAGNOSTIC_CHECKS = frozenset({"funnel", "settlement", "concentration", "paired", "probability", "payoff"})
CONCLUSIONS = ("supported", "contradicted", "inconclusive")


@dataclass(frozen=True)
class StudyBlock:
    id: str
    windows: Mapping[str, Window]

    def as_dict(self):
        return {"id": self.id, **{name: window.as_dict() for name, window in self.windows.items()}}


@dataclass(frozen=True)
class Study:
    proposal: str
    experiment: str
    round_id: str
    question: str
    checks: tuple[str, ...]
    interpretation: Mapping[str, str]
    blocks: tuple[StudyBlock, ...]
    payoff: Mapping[str, float] | None

    def as_dict(self):
        document = {"schema_version": STUDY_VERSION, "proposal": self.proposal,
                    "experiment": self.experiment, "round_id": self.round_id,
                    "question": self.question, "baseline": "control", "checks": list(self.checks),
                    "interpretation": dict(self.interpretation),
                    "blocks": [block.as_dict() for block in self.blocks]}
        if self.payoff is not None:
            document["payoff"] = dict(self.payoff)
        return document

    @property
    def digest(self):
        canonical = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                               allow_nan=False).encode()
        return hashlib.sha256(canonical).hexdigest()


def _blocks(value, protocol_windows):
    if value == "protocol":
        return (StudyBlock("reference", protocol_windows),)
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise Invalid("study.blocks", "use protocol ou uma lista ordenada de 1 a 64 blocos")
    result, seen = [], set()
    for index, raw in enumerate(value):
        field = f"study.blocks.{index}"
        raw = keys(raw, field, required=("id", "train", "calibration", "evaluation"))
        block_id = slug(raw["id"], f"{field}.id")
        if block_id in seen:
            raise Invalid(f"{field}.id", "ID de bloco repetido")
        windows = parse_windows({name: raw[name] for name in ("train", "calibration", "evaluation")}, field)
        if result and windows["evaluation"].start < result[-1].windows["evaluation"].end:
            raise Invalid(f"{field}.evaluation", "avaliações devem estar em ordem e sem sobreposição")
        seen.add(block_id)
        result.append(StudyBlock(block_id, MappingProxyType(windows)))
    return tuple(result)


def _payoff(value):
    value = keys(value, "study.payoff", required=("WIN", "LOSS", "TIE"))
    normalized = {}
    for outcome, amount in value.items():
        try:
            if type(amount) in (float, int) and isfinite(amount):
                normalized[outcome] = float(amount)
                continue
        except OverflowError:
            pass
        raise Invalid(f"study.payoff.{outcome}", "esperado retorno numérico finito por unidade")
    return MappingProxyType(normalized)


def parse_study(raw, proposal, *, experiment_id=None):
    raw = keys(raw, "study", required=("experiment",), optional=("schema_version", "proposal",
               "round_id", "question", "baseline", "checks", "interpretation", "blocks", "payoff"))
    name = slug(raw["experiment"], "study.experiment")
    if experiment_id is not None and name != experiment_id:
        raise Invalid("study.experiment", "não corresponde ao experimento solicitado")
    experiment = proposal.experiments.get(name)
    if experiment is None or experiment.managed_lineage is None:
        raise Invalid("study.experiment", "estudo exige experimento gerenciado existente")
    return parse_study_declaration(raw, proposal.id, name, experiment.windows)


def parse_study_declaration(raw, proposal_id, experiment_id, protocol_windows):
    """Validate a declaration against either the active experiment or its frozen identity."""
    fields = ("schema_version", "proposal", "experiment", "round_id", "question",
              "baseline", "checks", "interpretation", "blocks")
    raw = keys(raw, "study", required=fields, optional=("payoff",))
    require_version(raw, "study", STUDY_VERSION)
    if raw["proposal"] != proposal_id:
        raise Invalid("study.proposal", "não corresponde à proposta solicitada")
    if raw["experiment"] != experiment_id:
        raise Invalid("study.experiment", "não corresponde ao experimento solicitado")
    if raw["baseline"] != "control":
        raise Invalid("study.baseline", "use o braço control do experimento fixado")
    payoff = _payoff(raw["payoff"]) if "payoff" in raw else None
    checks = raw["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= len(DIAGNOSTIC_CHECKS):
        raise Invalid("study.checks", "esperada lista não vazia de verificações")
    seen = set()
    for index, check in enumerate(checks):
        if (not isinstance(check, str) or check not in DIAGNOSTIC_CHECKS or check in seen
                or (check == "payoff" and payoff is None)):
            raise Invalid(f"study.checks.{index}", "verificação desconhecida, repetida ou inaplicável")
        seen.add(check)
    criteria = keys(raw["interpretation"], "study.interpretation", required=CONCLUSIONS)
    interpretation = MappingProxyType({k: text(criteria[k], f"study.interpretation.{k}") for k in CONCLUSIONS})
    return Study(proposal_id, experiment_id, slug(raw["round_id"], "study.round_id"),
                 text(raw["question"], "study.question"), tuple(checks), interpretation,
                 _blocks(raw["blocks"], protocol_windows), payoff)


def load_study(path, proposal, *, experiment_id=None):
    try:
        raw = read_limited(Path(path))
    except OSError as exc:
        raise Invalid("study", f"não foi possível ler {path}: {exc}") from exc
    return parse_study(parse_yaml(raw, "study"), proposal, experiment_id=experiment_id)
