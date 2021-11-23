# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
import pytest
import yaml

from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "metacontroller-operator"


@pytest.mark.abort_on_fail
async def test_build_and_deploy_with_trust(ops_test: OpsTest):
    logger.info("Building charm")
    built_charm_path = await ops_test.build_charm("./")
    logger.info(f"Built charm {built_charm_path}")

    resources = {}
    for resource_name, resource_data in METADATA.get("resources", {}).items():
        image_path = resource_data["upstream-source"]
        resources[resource_name] = image_path

    logger.info(f"Deploying charm {APP_NAME} using resources '{resources}'")

    await ops_test.model.deploy(
        entity_url=built_charm_path,
        application_name=APP_NAME,
        resources=resources,
        trust=True,
    )

    apps = [APP_NAME]
    await ops_test.model.wait_for_idle(
        apps=apps, status="active", raise_on_blocked=True, timeout=300
    )
    for app_name in apps:
        for i_unit, unit in enumerate(ops_test.model.applications[app_name].units):
            assert (
                unit.workload_status == "active"
            ), f"Application {app_name}.Unit {i_unit}.workload_status != active"
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


# TODO: Add test for charm removal
# TODO: Add test that USES metacontroller for something (act on a namespace
#  given particular metadata, similar to kfp?)
