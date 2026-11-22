from importlib.resources import files
from types import SimpleNamespace

import pytest
import yaml

from tesse.forge.catalog import VOLATILITY_SCORE
from tesse.forge.contracts import Experiment, Invalid
from tesse.forge.graph import compile_graph, require_supported_route
from tesse.forge.runner import preflight_study
from tesse.forge.validation import validate_documents

EXPERIMENT_ID = "edge-vs-raw"
EXPERIMENT_PATH = f"protocol.yaml.experiments.{EXPERIMENT_ID}"


@pytest.fixture
def documents():
    templates = files("tesse.forge").joinpath("templates")
    proposal = templates.joinpath("proposal.yaml").read_bytes()
    protocol = yaml.safe_load(templates.joinpath("protocol.yaml").read_bytes())
    experiment = protocol["experiments"][EXPERIMENT_ID]
    experiment["snapshot"] = {
        "manifest": "nonexistent.json",
        "manifest_sha256": "1" * 64,
        "sha256": "2" * 64,
    }
    return proposal, protocol, experiment


def _validate(documents):
    proposal, protocol, _ = documents
    return validate_documents(proposal, yaml.safe_dump(protocol).encode())


def _use_managed(experiment):
    experiment["managed"] = {
        "schema_version": 1,
        "lineage": {"source": "fixture", "symbol": "BTCUSDT", "timeframe": "M1"},
    }


def _use_xgboost(experiment):
    nodes = experiment["nodes"]
    del nodes["margin"]
    nodes["fit"] = {"component": "learner.xgboost@1.0.0", "inputs": {"train": "split.train"}}
    nodes["predict"]["component"] = "predict.xgboost@1.0.0"


def _compile_unmanaged(experiment):
    nodes, result = compile_graph(
        experiment["nodes"], experiment["result"]["from"], f"{EXPERIMENT_PATH}.nodes", "Comparison"
    )
    return Experiment(EXPERIMENT_ID, experiment["snapshot"], {}, nodes, result, "comparison")


@pytest.mark.parametrize("standalone_study", [False, True])
def test_unmanaged_xgboost_is_structural_but_has_no_supported_route(documents, standalone_study):
    _, _, raw = documents
    _use_xgboost(raw)
    experiment = _compile_unmanaged(raw)

    with pytest.raises(Invalid, match="XGBoost exige"):
        require_supported_route(experiment, standalone_study=standalone_study)
    with pytest.raises(Invalid, match="sem rota suportada.*XGBoost") as error:
        _validate(documents)
    assert error.value.field == EXPERIMENT_PATH


@pytest.mark.parametrize("volatility", [False, True])
def test_managed_xgboost_allows_only_standalone_development(documents, volatility):
    _, _, raw = documents
    _use_managed(raw)
    _use_xgboost(raw)
    if volatility:
        raw["nodes"]["base"]["component"] = VOLATILITY_SCORE

    validation = _validate(documents)
    assert validation.valid(strict=True)  # No study or snapshot is needed to validate.
    experiment = validation.definition.experiments[EXPERIMENT_ID]
    require_supported_route(experiment, standalone_study=True)

    with pytest.raises(Invalid, match="run --study"):
        require_supported_route(experiment)
    with pytest.raises(Invalid, match="run --study"):
        preflight_study(
            experiment,
            SimpleNamespace(experiment=EXPERIMENT_ID),
            None,
            confirmation_window=object(),
        )


def test_volatility_without_xgboost_has_no_supported_route(documents):
    _, _, raw = documents
    raw["nodes"]["base"]["component"] = VOLATILITY_SCORE
    del raw["nodes"]["fit"]["params"]["monotonic_cst"]
    experiment = _compile_unmanaged(raw)

    for standalone_study in (False, True):
        with pytest.raises(Invalid, match="volatilidade exige XGBoost"):
            require_supported_route(experiment, standalone_study=standalone_study)
    with pytest.raises(Invalid, match="sem rota suportada.*volatilidade exige XGBoost") as error:
        _validate(documents)
    assert error.value.field == EXPERIMENT_PATH

    _use_managed(raw)
    with pytest.raises(Invalid, match="volatilidade exige o par learner/predict XGBoost"):
        _validate(documents)


@pytest.mark.parametrize("managed", [False, True])
def test_existing_hgb_remains_eligible(documents, managed):
    _, _, raw = documents
    if managed:
        _use_managed(raw)

    validation = _validate(documents)
    assert validation.valid(strict=True)
    require_supported_route(validation.definition.experiments[EXPERIMENT_ID])


def test_existing_dmi_comparison_remains_eligible(documents):
    _, _, raw = documents
    nodes = raw["nodes"]
    raw["nodes"] = {key: nodes[key] for key in ("data", "control", "candidate", "compare")}
    for arm, min_adx in (("control", 25), ("candidate", 30)):
        selection = f"dmi-{arm}"
        raw["nodes"][selection] = {
            "component": "selection.dmi_cross@1.0.0",
            "inputs": {"candles": "data.candles"},
            "params": {"min_adx": min_adx},
        }
        raw["nodes"][arm]["inputs"]["proposals"] = f"{selection}.proposals"

    validation = _validate(documents)
    assert validation.valid(strict=True)
    require_supported_route(validation.definition.experiments[EXPERIMENT_ID])
