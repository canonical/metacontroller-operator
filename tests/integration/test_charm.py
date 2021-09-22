import logging
from pathlib import Path
import pytest
import yaml

from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "metacontroller-operator"


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    logger.info("Building charm")
    built_charm_path = await ops_test.build_charm("./")
    logger.info(f"Built charm {built_charm_path}")

    resources = {}
    for resource_name, resource_data in METADATA["resources"].items():
        image_path = resource_data["upstream-source"]
        resources[resource_name] = image_path

    logger.info(f"Deploying charm {APP_NAME} using resources '{resources}'")

    await ops_test.model.deploy(
        entity_url=built_charm_path,
        application_name=APP_NAME,
        resources=resources
    )

    # TODO: There are more complete ways to do this - see other charms.  This passes at incorrect times.
    await ops_test.model.wait_for_idle(timeout=60 * 60)

    # TODO: confirm it actually deployed correctly
