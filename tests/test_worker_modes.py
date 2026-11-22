from types import SimpleNamespace

import pytest

from tesse.cli import main, parser
from tesse.config import Config
from tesse.execution.readiness import live_readiness_report


@pytest.mark.parametrize("command", ("start", "worker", "_worker"))
def test_retired_observation_flag_is_rejected_before_loading_config(command, tmp_path, capsys):
    config = tmp_path / "missing.yml"
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(config), command, "--observe-only"])
    assert exc.value.code == 2
    assert "--observe-only" in capsys.readouterr().err
    assert not config.exists()


@pytest.mark.parametrize("command", ("start", "_worker"))
def test_shadow_remains_an_explicit_simulation_option(command):
    assert parser().parse_args([command, "--shadow"]).shadow is True


@pytest.mark.parametrize(
    ("runtime_environment", "sdk_ready", "expected_blockers"),
    [
        ("TEST", False, ["sdk_orders_ready"]),
        ("REAL", True, ["environment"]),
        ("TEST", True, []),
    ],
)
def test_live_readiness_requires_test_and_order_ready_sdk(
    runtime_environment, sdk_ready, expected_blockers,
):
    config = Config()
    environment = SimpleNamespace(
        operation=config.operation,
        runtime=SimpleNamespace(environment=runtime_environment),
    )
    report = live_readiness_report(
        environment,
        provider_healthy=True,
        model_active=True,
        candles=True,
        broker_balance=True,
        signal_identity=True,
        snapshot_fresh=True,
        sdk_ready=sdk_ready,
    )
    assert report["blocked_by"] == expected_blockers
    assert report["state"] == ("blocked" if expected_blockers else "test_order_ready")
