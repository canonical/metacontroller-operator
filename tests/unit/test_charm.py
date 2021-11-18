# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from unittest import mock

from charm import MetacontrollerOperatorCharm
import lightkube.codecs
from lightkube.resources.apps_v1 import Deployment, StatefulSet
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.models.meta_v1 import ObjectMeta
from ops.model import WaitingStatus, ActiveStatus, BlockedStatus, MaintenanceStatus
from ops.testing import Harness
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture
def harness():
    return Harness(MetacontrollerOperatorCharm)


@pytest.fixture()
def harness_with_charm(harness):
    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()
    return harness


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


def test_render_yaml(harness_with_charm):
    manifests_root = Path("./tests/unit/render_yaml/")
    unrendered_yaml_file = "simple_manifest_input.yaml"
    expected_yaml_file = "simple_manifest_result.yaml"
    harness = harness_with_charm
    harness.charm.manifest_file_root = manifests_root

    assert harness.charm.manifest_file_root == Path(manifests_root)
    objs = harness.charm._render_yaml(unrendered_yaml_file)

    rendered_yaml_expected = (
        Path(manifests_root / expected_yaml_file).read_text().strip()
    )
    assert lightkube.codecs.dump_all_yaml(objs).strip() == rendered_yaml_expected


def test_create_controller(harness_with_charm, mocker):
    namespace = "test-namespace"

    mocked_render_controller_return = [
        Deployment(metadata=ObjectMeta(name="test-deployment", namespace=namespace)),
        StatefulSet(metadata=ObjectMeta(name="test-deployment", namespace=namespace)),
    ]
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._render_controller",
        return_value=mocked_render_controller_return,
    )

    mocked_lightkube_client = mock.Mock()
    mocked_lightkube_client_expected_calls = [
        mock.call.create(obj) for obj in mocked_render_controller_return
    ]

    harness = harness_with_charm
    harness.charm.lightkube_client = mocked_lightkube_client

    harness.charm._create_controller()
    mocked_lightkube_client.assert_has_calls(mocked_lightkube_client_expected_calls)


@pytest.mark.parametrize(
    "install_succeeded, expected_status",
    (
        (True, ActiveStatus()),
        (False, BlockedStatus("Some kubernetes resources missing/not ready")),
    ),
)
def test_install(harness_with_charm, mocker, install_succeeded, expected_status):
    mocker.patch("charm.MetacontrollerOperatorCharm._create_rbac")
    mocker.patch("charm.MetacontrollerOperatorCharm._create_crds")
    mocker.patch("charm.MetacontrollerOperatorCharm._create_controller")
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._check_deployed_resources",
        return_value=install_succeeded,
    )
    mocker.patch("time.sleep")

    harness = harness_with_charm
    harness.charm.on.install.emit()
    harness.charm._create_rbac.assert_called_once()
    harness.charm._create_crds.assert_called_once()
    harness.charm._create_controller.assert_called_once()
    assert harness.charm.model.unit.status == expected_status


@pytest.mark.parametrize(
    "deployed_resources_status, expected_status, n_install_calls",
    (
        (True, ActiveStatus(), 0),
        (
            False,
            MaintenanceStatus("Missing kubernetes resources detected - reinstalling"),
            1,
        ),
    ),
)
def test_update_status(
    harness_with_charm,
    mocker,
    deployed_resources_status,
    expected_status,
    n_install_calls,
):
    mocker.patch("charm.MetacontrollerOperatorCharm._install")
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._check_deployed_resources",
        return_value=deployed_resources_status,
    )

    harness = harness_with_charm
    harness.charm.on.update_status.emit()
    assert harness.charm.model.unit.status == expected_status

    # Assert install called correct number of times
    assert harness.charm._install.call_count == n_install_calls


def fake_get_k8s_obj_always_successful(obj, _):
    """Mock of get_k8s_obj that always succeeds, returning the requested lightkube object"""
    return obj


def fake_get_k8s_obj_crds_fail(obj, _):
    """Mock of get_k8s_obj that fails if getting a crd, otherwise succeeds"""
    # Not sure why lightkube doesn't use their obj.kind.  Use isinstance as a fix
    if isinstance(obj, lightkube.resources.apiextensions_v1.CustomResourceDefinition):
        raise _FakeApiError()
    else:
        return obj


@pytest.fixture()
def mock_resources_ready():
    # TODO: When testing stateful sets for scale, add scale to this statefulset
    return [
        StatefulSet(metadata=ObjectMeta(name="ss1", namespace="namespace")),
        CustomResourceDefinition(metadata=ObjectMeta(name="crd1"), spec={}),
    ]


@pytest.mark.parametrize(
    "mock_get_k8s_obj,mock_resources_fixture,expected_are_resources_ok",
    (
        (fake_get_k8s_obj_always_successful, "mock_resources_ready", True),
        (fake_get_k8s_obj_crds_fail, "mock_resources_ready", False),
        # (fake_get_k8s_statefulsets_have_no_replicas, mock_resources_not_ready, False),  # TODO: Add when ss check enabled
    ),
)
def test_check_deployed_resources(
    request,
    mocker,
    harness_with_charm,
    mock_get_k8s_obj,
    mock_resources_fixture,
    expected_are_resources_ok,
):
    mocker.patch("charm.get_k8s_obj", side_effect=mock_get_k8s_obj)

    # Get the value of the fixture passed as a param, otherwise it is just the fixture itself
    mock_resources = request.getfixturevalue(mock_resources_fixture)
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._render_all_resources",
        return_value=mock_resources,
    )

    harness = harness_with_charm
    are_resources_ok = harness.charm._check_deployed_resources()
    assert are_resources_ok == expected_are_resources_ok

    # TODO: Assert on the logs emitted?


class _FakeResponse:
    """Used to fake an httpx response during testing only."""

    def __init__(self, code):
        self.code = code
        self.name = ""

    def json(self):
        reason = ""
        if self.code == 409:
            reason = "AlreadyExists"
        return {
            "apiVersion": 1,
            "code": self.code,
            "message": "broken",
            "reason": reason,
        }


class _FakeApiError(lightkube.core.exceptions.ApiError):
    """Used to simulate an ApiError during testing."""

    def __init__(self, code=401):
        super().__init__(response=_FakeResponse(code))
