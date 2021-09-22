import logging
from pathlib import Path

import pytest
import yaml

from ops.model import WaitingStatus

from ops.testing import Harness
from charm import MetacontrollerOperatorCharm

logger = logging.getLogger(__name__)


@pytest.fixture
def harness():
    return Harness(MetacontrollerOperatorCharm)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


def test_render_manifests(harness):
    manifests_root = Path("./tests/unit/render_manifests/")
    manifests_input_path = manifests_root / "input_simple/"
    # NOTE: imagename substitution is currently hard coded in charm.py, so updating image requires
    # us to update result.yaml as well
    manifests_rendered = manifests_root / "result_simple.yaml"

    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()
    harness.charm.manifest_file_root = manifests_input_path

    assert harness.charm.manifest_file_root == manifests_input_path

    manifests, manifests_str = harness.charm._render_manifests()
    expected_manifests_str = manifests_rendered.read_text()

    assert manifests_str.strip() == expected_manifests_str.strip()


@pytest.mark.parametrize(
    "case",
    [
        "simple",
    ],
)
def test_install_minimal(harness, mocker, case):
    subprocess_run = mocker.patch("subprocess.run")

    # Mock check_deployed_resources so we don't try to load/use k8s api
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._check_deployed_resources",
        return_value=True,
    )

    manifests_root = Path("./tests/unit/render_manifests/")
    manifests_input_path = manifests_root / f"input_{case}/"
    manifests_rendered = manifests_root / f"result_{case}.yaml"

    harness.set_model_name("test-namespace")
    harness.set_leader(True)
    harness.begin()
    harness.charm.manifest_file_root = manifests_input_path

    harness.charm._install("ignored_event")
    logger.info(
        f"subprocess.run.call_args_list (after) = {subprocess_run.call_args_list}"
    )

    expected_args = (["./kubectl", "apply", "-f-"],)
    expected_kwarg_input = list(yaml.safe_load_all(manifests_rendered.read_text()))

    call_args = subprocess_run.call_args_list
    actual_args = call_args[0].args
    actual_kwarg_input = list(yaml.safe_load_all(call_args[0].kwargs["input"]))

    assert len(call_args) == 1

    assert actual_args == expected_args
    assert actual_kwarg_input == expected_kwarg_input


# TODO: Create test that "deploys" manifests then simulates the watchers
