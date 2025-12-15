# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest
import yaml
from charmed_kubeflow_chisme.testing import (
    assert_alert_rules,
    assert_metrics_endpoint,
    assert_security_context,
    deploy_and_assert_grafana_agent,
    generate_container_securitycontext_map,
    get_alert_rules,
    get_pod_names,
)
from charms_dependencies import ADMISSION_WEBHOOK
from lightkube import Client
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
METACONTROLLER_YAML = yaml.safe_load(Path("./src/files/manifests/metacontroller.yaml").read_text())
APP_NAME = "metacontroller-operator"


@pytest.fixture(scope="session")
def lightkube_client() -> Client:
    """Returns lightkube Kubernetes client"""
    client = Client(field_manager=f"{APP_NAME}")
    return client


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


async def test_metrics_endpoint(ops_test: OpsTest):
    """Test metrics_endpoints are defined in relation data bag and their accessibility.
    This function gets all the metrics_endpoints from the relation data bag, checks if
    they are available from the grafana-agent-k8s charm and finally compares them with the
    ones provided to the function.
    """
    app = ops_test.model.applications[APP_NAME]
    await assert_metrics_endpoint(app, metrics_port=9999, metrics_path="/metrics")


async def test_alert_rules(ops_test: OpsTest):
    """Test check charm alert rules and rules defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    alert_rules = get_alert_rules()
    logger.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)


async def kubectl_can_i(
    ops_test: OpsTest, action: str, resource: str, namespace: str, service_account: str
) -> bool:
    """Run kubectl auth can-i command for given action and resource, as given service account."""
    logger.info("Checking with `kubectl auth can-i create`")
    _, stdout, _ = await ops_test.run(
        "kubectl",
        "auth",
        "can-i",
        action,
        resource,
        f"--as=system:serviceaccount:{namespace}:{service_account}",
        check=True,
        fail_msg="Failed to test listing resources rbac permissions with kubectl auth",
    )
    return stdout.strip() == "yes"


async def test_authorization_for_creating_resources(ops_test: OpsTest):
    """Assert Metacontroller can create K8s resources."""

    # Needed for Resource Dispatcher
    necessary_permissions = [
        (resource, action)
        for resource in [
            "secrets",
            "services",
            "serviceaccounts",
            "pods",
            "poddefaults",
            "configmaps",
            "roles",
            "rolebindings",
        ]
        for action in ["get", "list", "watch", "create", "update", "patch", "delete"]
    ]
    necessary_permissions.extend(
        [
            (resource, "deletecollection")
            for resource in ["services", "serviceaccounts", "pods", "configmaps"]
        ]
    )
    namespace = ops_test.model_name

    for resource, action in necessary_permissions:
        assert await kubectl_can_i(
            ops_test=ops_test,
            action=action,
            resource=resource,
            namespace=namespace,
            service_account=f"{APP_NAME}-charm",
        )


def build_pod_container_map(model_name: str, metacontroller_template: dict) -> dict[str, dict]:
    """Build full map of pods:containers belonging to this charm.

    This function builds a custom mapping of security context for pods and containers,
    necessary because some pods are not directly spawned by juju but are defined in
    `src/files/manifests/metacontroller.yaml`.
    """
    charm_pods: list = get_pod_names(model_name, APP_NAME)
    statefulset_pods: list = get_pod_names(model_name, f"{model_name}-{APP_NAME}-charm")
    metacontroller_container_name = metacontroller_template["spec"]["template"]["spec"][
        "containers"
    ][0]["name"]
    metacontroller_container_security_context = metacontroller_template["spec"]["template"][
        "spec"
    ]["containers"][0]["securityContext"]
    pod_container_map = {}

    for charm_pod in charm_pods:
        pod_container_map[charm_pod] = generate_container_securitycontext_map(METADATA)
    for pod in statefulset_pods:
        pod_container_map[pod] = {
            metacontroller_container_name: metacontroller_container_security_context
        }
    return pod_container_map


async def test_container_security_context(
    ops_test: OpsTest,
    lightkube_client: Client,
):
    """Test container security context is correctly set.

    Verify that container spec defines the security context with correct
    user ID and group ID.
    """
    failed_checks = []
    pod_container_map = build_pod_container_map(ops_test.model_name, METACONTROLLER_YAML)
    for pod, pod_containers in pod_container_map.items():
        for container in pod_containers.keys():
            try:
                logger.info("Checking security context for container %s (pod: %s)", container, pod)
                assert_security_context(
                    lightkube_client,
                    pod,
                    container,
                    pod_containers,
                    ops_test.model_name,
                )
            except AssertionError as err:
                failed_checks.append(f"{pod}/{container}: {err}")
    assert failed_checks == []


# TODO: Add test for charm removal
# TODO: Add test that USES metacontroller for something (act on a namespace
#  given particular metadata, similar to kfp?)
# TODO: Add test that makes sure the Grafana relation actually works
#  once the template is defined.
