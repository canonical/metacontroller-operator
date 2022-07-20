# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import nullcontext as does_not_raise
import logging
from pathlib import Path
from unittest import mock

from charm import MetacontrollerOperatorCharm, CheckFailed
import lightkube.codecs
from lightkube.resources.apps_v1 import Deployment, StatefulSet
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.models.apps_v1 import StatefulSetSpec, StatefulSetStatus
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


def test_render_resource(harness_with_charm):
    manifest_root = Path("./tests/unit/render_resource/")
    unrendered_yaml_file = "simple_manifest_input.yaml"
    expected_yaml_file = "simple_manifest_result.yaml"
    harness = harness_with_charm
    harness.charm._manifest_file_root = manifest_root
    harness.charm._resource_files = {"test_yaml": unrendered_yaml_file}
    harness.charm._metacontroller_image = "sample/metacontroller:tag"

    assert harness.charm._manifest_file_root == Path(manifest_root)

    objs = harness.charm._render_resource("test_yaml")
    rendered_yaml_expected = (
        Path(manifest_root / expected_yaml_file).read_text().strip()
    )
    assert lightkube.codecs.dump_all_yaml(objs).strip() == rendered_yaml_expected


def test_create_controller(harness_with_charm, mocker):
    namespace = "test-namespace"

    mocked_render_controller_return = [
        Deployment(metadata=ObjectMeta(name="test-deployment", namespace=namespace)),
        StatefulSet(metadata=ObjectMeta(name="test-deployment", namespace=namespace)),
    ]
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._render_resource",
        return_value=mocked_render_controller_return,
    )

    mocked_lightkube_client = mock.Mock()
    mocked_lightkube_client_expected_calls = [
        mock.call.create(obj) for obj in mocked_render_controller_return
    ]

    harness = harness_with_charm
    harness.charm.lightkube_client = mocked_lightkube_client

    harness.charm._create_resource("dummy-name")
    mocked_lightkube_client.assert_has_calls(mocked_lightkube_client_expected_calls)


def returns_true(*args, **kwargs):
    return True


@pytest.mark.parametrize(
    "install_side_effect, expected_charm_status",
    (
        (returns_true, ActiveStatus()),
        (
            CheckFailed("", BlockedStatus),
            BlockedStatus(
                "Some Kubernetes resources did not start correctly during install"
            ),
        ),
    ),
)
def test_install(
    harness_with_charm, mocker, install_side_effect, expected_charm_status
):
    mocker.patch("charm.MetacontrollerOperatorCharm._create_resource")
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._check_deployed_resources",
        side_effect=install_side_effect,
    )
    mocker.patch("time.sleep")

    expected_calls = [
        mock.call(resource_name) for resource_name in ["rbac", "crds", "controller"]
    ]

    harness = harness_with_charm

    # Fail fast
    harness.charm._max_time_checking_resources = 0.5

    harness.charm.on.install.emit()

    harness.charm._create_resource.assert_has_calls(expected_calls)
    assert harness.charm.model.unit.status == expected_charm_status


@pytest.mark.parametrize(
    "check_deployed_resources_side_effect, expected_status, n_install_calls",
    (
        (returns_true, ActiveStatus(), 0),
        (
            CheckFailed("", WaitingStatus),
            MaintenanceStatus("Missing kubernetes resources detected - reinstalling"),
            1,
        ),
    ),
)
def test_update_status(
    harness_with_charm,
    mocker,
    check_deployed_resources_side_effect,
    expected_status,
    n_install_calls,
):
    mocker.patch("charm.MetacontrollerOperatorCharm._install")
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._check_deployed_resources",
        side_effect=check_deployed_resources_side_effect,
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
def resources_that_are_ok():
    """List of lightkube resources that are ok"""
    return [
        StatefulSet(
            metadata=ObjectMeta(name="ss1", namespace="namespace"),
            spec=StatefulSetSpec(replicas=3, selector="", serviceName="", template=""),
            status=StatefulSetStatus(replicas=3, readyReplicas=3),
        ),
        CustomResourceDefinition(metadata=ObjectMeta(name="crd1"), spec={}),
    ]


@pytest.fixture()
def resources_with_apierror():
    """List of lightkube resources that includes an exception"""
    return [
        StatefulSet(
            metadata=ObjectMeta(name="ss1", namespace="namespace"),
            spec=StatefulSetSpec(replicas=3, selector="", serviceName="", template=""),
            status=StatefulSetStatus(replicas=3, readyReplicas=3),
        ),
        _FakeApiError(),
    ]


@pytest.fixture()
def resources_with_unready_statefulset():
    """
    List of lightkube resources that has a StatefulSet that does not have all replicas available
    """
    return [
        StatefulSet(
            metadata=ObjectMeta(name="ss1", namespace="namespace"),
            spec=StatefulSetSpec(replicas=3, selector="", serviceName="", template=""),
            status=StatefulSetStatus(replicas=0, readyReplicas=0),
        ),
    ]


@pytest.mark.parametrize(
    "render_all_resources_side_effect_fixture,lightkube_get_side_effect_fixture,expected_result",
    (
        # Test where objects exist in k8s
        ("resources_that_are_ok", "resources_that_are_ok", does_not_raise()),
        # Test where objects do not exist in k8s
        (
            "resources_that_are_ok",
            "resources_with_apierror",
            pytest.raises(CheckFailed),
        ),
        # Test with StatefulSet that does not have all replicas
        (
            "resources_with_unready_statefulset",
            "resources_with_unready_statefulset",
            pytest.raises(CheckFailed),
        ),
    ),
)
def test_check_deployed_resources(
    request,
    mocker,
    harness_with_charm,
    render_all_resources_side_effect_fixture,
    lightkube_get_side_effect_fixture,
    expected_result,
):
    # Mock _render_all_resources
    # Get the value of the fixture passed as a param, otherwise it is just the fixture itself
    # Resources for render_all_resources are assigned to return_value so it returns the entire list
    # of resources in a single call
    render_all_resources_side_effect = request.getfixturevalue(
        render_all_resources_side_effect_fixture
    )
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._render_all_resources",
        return_value=render_all_resources_side_effect,
    )

    # Mock charm's lightkube client
    # Resources for mock of client.get are assigned to side_effect so a single resource is returned
    # for each call
    lightkube_get_side_effect_fixture = request.getfixturevalue(
        lightkube_get_side_effect_fixture
    )
    mock_client = mock.MagicMock()
    mock_client.get.side_effect = lightkube_get_side_effect_fixture

    harness = harness_with_charm
    harness.charm._lightkube_client = mock_client

    with expected_result:
        harness.charm._check_deployed_resources()


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
