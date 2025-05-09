# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest
import yaml
from charmed_kubeflow_chisme.testing import (
    assert_alert_rules,
    assert_metrics_endpoint,
    deploy_and_assert_grafana_agent,
    get_alert_rules,
)
from charms_dependencies import ADMISSION_WEBHOOK
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "metacontroller-operator"


@pytest.mark.abort_on_fail
async def test_build_and_deploy_with_trust(ops_test: OpsTest):
    # Deploy the Admission Webhook, to ensure PodDefault CRs are installed
    await ops_test.model.deploy(
        entity_url=ADMISSION_WEBHOOK.charm,
        channel=ADMISSION_WEBHOOK.channel,
        trust=ADMISSION_WEBHOOK.trust,
    )
    await ops_test.model.wait_for_idle(
        apps=[ADMISSION_WEBHOOK.charm], status="active", timeout=60 * 15
    )

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

    # Deploying grafana-agent-k8s and add all relations
    await deploy_and_assert_grafana_agent(
        ops_test.model, APP_NAME, metrics=True, dashboard=True, logging=False
    )


async def test_metrics_enpoint(ops_test):
    """Test metrics_endpoints are defined in relation data bag and their accessibility.
    This function gets all the metrics_endpoints from the relation data bag, checks if
    they are available from the grafana-agent-k8s charm and finally compares them with the
    ones provided to the function.
    """
    app = ops_test.model.applications[APP_NAME]
    await assert_metrics_endpoint(app, metrics_port=9999, metrics_path="/metrics")


async def test_alert_rules(ops_test):
    """Test check charm alert rules and rules defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    alert_rules = get_alert_rules()
    logger.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)


async def test_authorization_for_creating_resources(ops_test: OpsTest):
    """Assert Metacontroller can create PodDefaults, Secrets and ServiceAccounts."""
    logger.info("Checking with `kubectl auth can-i create`")

    # Needed for Resource Dispatcher
    resources = ["secrets", "serviceaccounts", "poddefaults"]
    namespace = ops_test.model_name

    for resource in resources:
        _, stdout, _ = await ops_test.run(
            "kubectl",
            "auth",
            "can-i",
            "list",
            f"{resource}",
            f"--as=system:serviceaccount:{namespace}:{APP_NAME}-charm",
            check=True,
            fail_msg="Failed to test listing resources rbac permissions with kubectl auth",
        )

        _, stdout, _ = await ops_test.run(
            "kubectl",
            "auth",
            "can-i",
            "get",
            f"{resource}",
            f"--as=system:serviceaccount:{namespace}:{APP_NAME}-charm",
            check=True,
            fail_msg="Failed to test getting resources rbac permissions with kubectl auth",
        )

        _, stdout, _ = await ops_test.run(
            "kubectl",
            "auth",
            "can-i",
            "create",
            f"{resource}",
            f"--as=system:serviceaccount:{namespace}:{APP_NAME}-charm",
            check=True,
            fail_msg="Failed to test creating resources rbac permissions with kubectl auth",
        )
        assert stdout.strip() == "yes"


# TODO: Add test for charm removal
# TODO: Add test that USES metacontroller for something (act on a namespace
#  given particular metadata, similar to kfp?)
# TODO: Add test that makes sure the Grafana relation actually works
#  once the template is defined.
