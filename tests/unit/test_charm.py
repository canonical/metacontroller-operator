# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from unittest import mock

from charm import MetacontrollerOperatorCharm
import lightkube.codecs
from lightkube.resources.apps_v1 import Deployment, StatefulSet
from lightkube.models.meta_v1 import ObjectMeta
from ops.model import WaitingStatus
from ops.testing import Harness
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture
def harness():
    return Harness(MetacontrollerOperatorCharm)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


def test_render_yaml(harness):
    manifests_root = Path("./tests/unit/render_yaml/")
    unrendered_yaml_file = "simple_manifest_input.yaml"
    expected_yaml_file = "simple_manifest_result.yaml"
    harness.set_leader(True)
    harness.set_model_name("test-namespace")
    harness.begin()
    harness.charm.manifest_file_root = manifests_root

    assert harness.charm.manifest_file_root == Path(manifests_root)
    objs = harness.charm._render_yaml(unrendered_yaml_file)

    rendered_yaml_expected = (
        Path(manifests_root / expected_yaml_file).read_text().strip()
    )
    assert lightkube.codecs.dump_all_yaml(objs).strip() == rendered_yaml_expected


def test_create_controller(harness, mocker):
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

    harness.set_leader(True)
    harness.set_model_name(namespace)
    harness.begin()
    harness.charm.lightkube_client = mocked_lightkube_client

    harness.charm._create_controller()
    mocked_lightkube_client.assert_has_calls(mocked_lightkube_client_expected_calls)


# TODO: Create test that "deploys" manifests then simulates the watchers
