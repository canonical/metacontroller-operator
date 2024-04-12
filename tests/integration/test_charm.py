# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from pathlib import Path

import pytest
import requests
import tenacity
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = "metacontroller-operator"
PROMETHEUS = "prometheus-k8s"
PROMETHEUS_CHANNEL = "latest/stable"
PROMETHEUS_TRUST = True
GRAFANA = "grafana-k8s"
GRAFANA_CHANNEL = "latest/stable"
GRAFANA_TRUST = True
PROMETHEUS_SCRAPE = "prometheus-scrape-config-k8s"
PROMETHEUS_SCRAPE_CHANNEL = "latest/stable"


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


async def test_prometheus_grafana_integration(ops_test: OpsTest):
    """Deploy prometheus, grafana and required relations, then test the metrics."""
    scrape_config = {"scrape_interval": "30s"}

    # Deploy and relate prometheus
    await ops_test.juju(
        "deploy",
        PROMETHEUS,
        "--channel",
        PROMETHEUS_CHANNEL,
        "--trust",
        check=True,
    )
    await ops_test.juju(
        "deploy",
        GRAFANA,
        "--channel",
        GRAFANA_CHANNEL,
        "--trust",
        check=True,
    )
    await ops_test.model.deploy(
        PROMETHEUS_SCRAPE,
        channel=PROMETHEUS_SCRAPE_CHANNEL,
        config=scrape_config,
    )

    await ops_test.model.add_relation(APP_NAME, PROMETHEUS_SCRAPE)
    await ops_test.model.add_relation(
        f"{PROMETHEUS}:grafana-dashboard", f"{GRAFANA}:grafana-dashboard"
    )
    await ops_test.model.add_relation(
        f"{APP_NAME}:grafana-dashboard", f"{GRAFANA}:grafana-dashboard"
    )
    await ops_test.model.add_relation(
        f"{PROMETHEUS}:metrics-endpoint",
        f"{PROMETHEUS_SCRAPE}:metrics-endpoint",
    )

    await ops_test.model.wait_for_idle(status="active", timeout=60 * 20)

    status = await ops_test.model.get_status()
    prometheus_unit_ip = status["applications"][PROMETHEUS]["units"][f"{PROMETHEUS}/0"]["address"]
    logger.info(f"Prometheus available at http://{prometheus_unit_ip}:9090")

    for attempt in retry_for_5_attempts:
        logger.info(
            f"Testing prometheus deployment (attempt " f"{attempt.retry_state.attempt_number})"
        )
        with attempt:
            r = requests.get(
                f"http://{prometheus_unit_ip}:9090/api/v1/query?"
                f'query=up{{juju_application="{APP_NAME}"}}'
            )
            response = json.loads(r.content.decode("utf-8"))
            response_status = response["status"]
            logger.info(f"Response status is {response_status}")
            assert response_status == "success"

            response_metric = response["data"]["result"][0]["metric"]
            assert response_metric["juju_application"] == APP_NAME
            assert response_metric["juju_model"] == ops_test.model_name

            # Assert the unit is available by checking the query result
            # The data is presented as a list [1707357912.349, '1'], where the
            # first value is a timestamp and the second value is the state of the unit
            # 1 means available, 0 means unavailable
            assert response["data"]["result"][0]["value"][1] == "1"


# Helper to retry calling a function over 30 seconds or 5 attempts
retry_for_5_attempts = tenacity.Retrying(
    stop=(tenacity.stop_after_attempt(5) | tenacity.stop_after_delay(30)),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)


# TODO: Add test for charm removal
# TODO: Add test that USES metacontroller for something (act on a namespace
#  given particular metadata, similar to kfp?)
# TODO: Add test that makes sure the Grafana relation actually works
#  once the template is defined.
