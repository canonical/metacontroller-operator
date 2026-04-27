# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from contextlib import nullcontext as does_not_raise
from pathlib import Path
from unittest import mock

import lightkube.codecs
import pytest
from lightkube.models.apps_v1 import StatefulSetSpec, StatefulSetStatus
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apps_v1 import Deployment, StatefulSet
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Harness

from charm import CheckFailed, MetacontrollerOperatorCharm

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
    rendered_yaml_expected = Path(manifest_root / expected_yaml_file).read_text().strip()
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
            BlockedStatus("Some Kubernetes resources did not start correctly during install"),
        ),
    ),
)
def test_install(harness_with_charm, mocker, install_side_effect, expected_charm_status):
    mocker.patch("charm.MetacontrollerOperatorCharm._create_resource")
    mocker.patch(
        "charm.MetacontrollerOperatorCharm._check_deployed_resources",
        side_effect=install_side_effect,
    )
    mocker.patch("time.sleep")

    expected_calls = [mock.call(resource_name) for resource_name in ["rbac", "crds", "controller"]]

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
    lightkube_get_side_effect_fixture = request.getfixturevalue(lightkube_get_side_effect_fixture)
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


@mock.patch("charm.lightkube.Client")
@mock.patch("charm.ServiceMeshConsumer")
@mock.patch("charm.PolicyResourceManager")
def test_reconcile_policy_resource_manager_with_mesh(
    mock_policy_manager_class: mock.MagicMock,
    mock_service_mesh: mock.MagicMock,
    mock_client: mock.MagicMock,
    harness: Harness,
):
    """Test _reconcile_policy_resource_manager when service-mesh relation is present."""
    # Mock _relation property to indicate a relation exists
    mock_mesh_instance = mock_service_mesh.return_value
    mock_mesh_instance._relation = mock.MagicMock()  # Relation exists
    mock_mesh_instance.mesh_type = "istio"

    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()

    # Mock the policy resource manager instance
    mock_policy_manager = mock_policy_manager_class.return_value

    harness.charm._reconcile_policy_resource_manager()

    # Verify reconcile was called with correct parameters
    mock_policy_manager.reconcile.assert_called_with(
        policies=[], mesh_type="istio", raw_policies=[harness.charm._allow_all_policy]
    )


@mock.patch("charm.lightkube.Client")
@mock.patch("charm.ServiceMeshConsumer")
@mock.patch("charm.PolicyResourceManager")
def test_reconcile_policy_resource_manager_without_mesh(
    mock_policy_manager_class: mock.MagicMock,
    mock_service_mesh: mock.MagicMock,
    mock_client: mock.MagicMock,
    harness: Harness,
):
    """Test _reconcile_policy_resource_manager when service-mesh relation is not present."""
    # Mock _relation property to return None (no relation established)
    mock_mesh_instance = mock_service_mesh.return_value
    mock_mesh_instance._relation = None

    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()

    # Mock the policy resource manager instance
    mock_policy_manager = mock_policy_manager_class.return_value

    harness.charm._reconcile_policy_resource_manager()

    # Verify reconcile was NOT called when there's no service-mesh relation
    mock_policy_manager.reconcile.assert_not_called()


@mock.patch("charm.lightkube.Client")
@mock.patch("charm.ServiceMeshConsumer")
@mock.patch("charm.PolicyResourceManager")
def test_on_remove_calls_remove_authorization_policies(
    mock_policy_manager_class: mock.MagicMock,
    mock_service_mesh: mock.MagicMock,
    mock_client: mock.MagicMock,
    harness: Harness,
):
    """Test that _on_remove calls _remove_authorization_policies."""
    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()

    # Mock the policy resource manager instance
    mock_policy_manager = mock_policy_manager_class.return_value

    harness.charm._on_remove(None)

    # Verify _remove_authorization_policies was called (which calls delete)
    mock_policy_manager.delete.assert_called()


@mock.patch("charm.lightkube.Client")
@mock.patch("charm.ServiceMeshConsumer")
@mock.patch("charm.PolicyResourceManager")
def test_service_mesh_relation_broken(
    mock_policy_manager_class: mock.MagicMock,
    mock_service_mesh: mock.MagicMock,
    mock_client: mock.MagicMock,
    harness: Harness,
):
    """Test that service-mesh relation broken event removes authorization policies."""
    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()

    # Mock the policy resource manager instance
    mock_policy_manager = mock_policy_manager_class.return_value

    # Add a service-mesh relation
    relation_id = harness.add_relation("service-mesh", "istio-beacon-k8s")
    harness.add_relation_unit(relation_id, "istio-beacon-k8s/0")

    # Break the relation
    harness.remove_relation(relation_id)

    # Verify that delete was called when relation was broken
    mock_policy_manager.delete.assert_called()
