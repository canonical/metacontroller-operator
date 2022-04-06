#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
from typing import Optional

import logging
from pathlib import Path

from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, MaintenanceStatus, BlockedStatus

import lightkube
from lightkube import codecs
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.core.exceptions import ApiError
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_delay,
    wait_exponential,
)


METACONTROLLER_IMAGE = "metacontroller/metacontroller:v0.3.0"
METRICS_PATH = "/metrics"
METRICS_PORT = "9999"


class MetacontrollerOperatorCharm(CharmBase):
    """Charm the Metacontroller"""

    def __init__(self, *args):
        super().__init__(*args)

        if not self.unit.is_leader():
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

        self.prometheus_provider = MetricsEndpointProvider(
            charm=self,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "metrics_path": METRICS_PATH,
                    "static_configs": [
                        {"targets": ["*:{}".format(METRICS_PORT)]}
                    ],
                }
            ],
        )

        self.dashboard_provider = GrafanaDashboardProvider(
            charm=self,
            relation_name="grafana-dashboard",
        )

        self.framework.observe(self.on.install, self._install)
        self.framework.observe(self.on.remove, self._remove)
        self.framework.observe(self.on.update_status, self._update_status)

        self.logger: logging.Logger = logging.getLogger(__name__)

        self._name: str = self.model.app.name
        self._namespace: str = self.model.name
        self._metacontroller_image: str = METACONTROLLER_IMAGE
        self._resource_files: dict = {
            "crds": "metacontroller-crds-v1.yaml",
            "rbac": "metacontroller-rbac.yaml",
            "controller": "metacontroller.yaml",
        }

        # TODO: Fix file imports and move ./src/files back to ./files
        self._manifest_file_root: Path = Path("./src/files/manifests/")

        self._lightkube_client: Optional[lightkube.Client] = None
        self._max_time_checking_resources = 150

    def _install(self, event):
        """Creates k8s resources required for the charm, patching over any existing ones it finds"""
        self.logger.info("Installing by instantiating Kubernetes objects")
        self.unit.status = MaintenanceStatus("Instantiating Kubernetes objects")

        # create rbac
        try:
            self._create_resource("rbac")
        except ApiError as e:
            # Handle forbidden error as this likely means we don't have --trust
            if e.status.code == 403:
                self.logger.error(
                    "Received Forbidden (403) error from lightkube when creating required RBAC.  "
                    "This may be due to the charm lacking permissions to create cluster-scoped "
                    "roles and resources.  Charm must be deployed with `--trust`"
                )
                self.logger.error(f"Error received: {str(e)}")
                self.unit.status = BlockedStatus(
                    "Cannot create required RBAC.  Charm may not have `--trust`"
                )
                return
            else:
                raise e

        self._create_resource("crds")

        self._create_resource("controller")

        self.logger.info("Waiting for installed Kubernetes objects to be operational")

        self.logger.info(
            f"Checking status of charm deployment in Kubernetes "
            f"(retrying for maximum {self._max_time_checking_resources}s)"
        )
        try:
            for attempt in Retrying(
                retry=retry_if_exception_type(CheckFailed),
                stop=stop_after_delay(max_delay=self._max_time_checking_resources),
                wait=wait_exponential(multiplier=0.1, min=0.1, max=15),
                reraise=True,
            ):
                with attempt:
                    self.logger.info(
                        f"Trying attempt {attempt.retry_state.attempt_number}"
                    )
                    self._check_deployed_resources()
        except CheckFailed:
            self.unit.status = BlockedStatus(
                "Some Kubernetes resources did not start correctly during install"
            )
            return

        self.logger.info("Resources detected as running")
        self.logger.info("Install successful")
        self.unit.status = ActiveStatus()
        return

    def _update_status(self, event):
        self.logger.info("Comparing current state to desired state")

        try:
            self._check_deployed_resources()
        except CheckFailed:
            self.logger.info(
                "Resources are missing.  Triggering install to reconcile resources"
            )
            self.unit.status = MaintenanceStatus(
                "Missing kubernetes resources detected - reinstalling"
            )
            self._install(event)
            return

        self.logger.info("Resources are ok.  Unit in ActiveStatus")
        self.unit.status = ActiveStatus()
        return

    def _remove(self, _):
        """Remove charm"""
        raise NotImplementedError()

    def _create_resource(self, resource_name, if_exists="patch"):
        self.logger.info(f"Applying manifests for {resource_name}")
        objs = self._render_resource(resource_name)
        create_all_lightkube_objects(
            objs, if_exists=if_exists, lightkube_client=self.lightkube_client
        )

    def _render_resource(self, yaml_name: [str, Path]):
        """Returns a list of lightkube k8s objects for a yaml file, rendered in charm context"""
        context = {
            "app_name": self._name,
            "namespace": self._namespace,
            "metacontroller_image": self._metacontroller_image,
        }
        with open(self._manifest_file_root / self._resource_files[yaml_name]) as f:
            return codecs.load_all_yaml(f, context=context)

    def _render_all_resources(self):
        """Returns a list of lightkube8s objects for all resources, rendered in charm context"""
        resources = []
        for resource_name in self._resource_files:
            resources.extend(self._render_resource(resource_name))
        return resources

    def _check_deployed_resources(self):
        """Check the status of deployed resources, returning True if ok else raising CheckFailed

        All abnormalities are captured in logs
        """
        expected_resources = self._render_all_resources()
        found_resources = [None] * len(expected_resources)
        errors = []

        self.logger.info("Checking for expected resources")
        for i, resource in enumerate(expected_resources):
            try:
                found_resources[i] = self.lightkube_client.get(
                    type(resource),
                    resource.metadata.name,
                    namespace=resource.metadata.namespace,
                )
            except lightkube.core.exceptions.ApiError:
                errors.append(
                    f"Cannot find k8s object for metadata '{resource.metadata}'"
                )

        self.logger.info("Checking readiness of found StatefulSets")
        statefulsets_ok, statefulsets_errors = validate_statefulsets(found_resources)
        errors.extend(statefulsets_errors)

        # Log any errors
        for err in errors:
            self.logger.info(err)

        if len(errors) == 0:
            return True
        else:
            raise CheckFailed(
                "Some Kubernetes resources missing/not ready.  See logs for details",
                WaitingStatus,
            )

    def _get_manifest_files(self) -> list:
        """Returns a list of all manifest files"""
        return glob.glob(str(self._manifest_file_root / "*.yaml"))

    @property
    def lightkube_client(self):
        if not self._lightkube_client:
            self._lightkube_client = lightkube.Client()
        return self._lightkube_client

    @lightkube_client.setter
    def lightkube_client(self, client):
        self._lightkube_client = client


def validate_statefulsets(objs):
    """Returns True if all StatefulSets in objs have the expected number of readyReplicas else False

    Optionally emits a message to logger for any StatefulSets that do not have their desired number
    of replicas

    Returns: Tuple of (Success [Boolean], Errors [list of str error messages]
    """
    errors = []

    for obj in objs:
        if isinstance(obj, StatefulSet):
            readyReplicas = obj.status.readyReplicas
            replicas_expected = obj.spec.replicas
            if readyReplicas != replicas_expected:
                message = (
                    f"StatefulSet {obj.metadata.name} in namespace "
                    f"{obj.metadata.namespace} has {readyReplicas} readyReplicas, "
                    f"expected {replicas_expected}"
                )
                errors.append(message)

    return len(errors) == 0, errors


ALLOWED_IF_EXISTS = (None, "replace", "patch")


def _validate_if_exists(if_exists):
    if if_exists in ALLOWED_IF_EXISTS:
        return if_exists
    else:
        raise ValueError(
            f"Invalid value for if_exists '{if_exists}'.  Must be one of {ALLOWED_IF_EXISTS}"
        )


def create_all_lightkube_objects(
    objs: [list, tuple],
    if_exists: [str, None] = None,
    lightkube_client: lightkube.Client = None,
    logger: Optional[logging.Logger] = None,
):
    """Creates all k8s resources listed in a YAML file via lightkube

    Args:
        objs (list, tuple): List of lightkube objects to create
        if_exists (str): If an object already exists, do one of:
            patch: Try to lightkube.patch the existing resource
            replace: Try to lightkube.replace the existing resource (not yet implemented)
            None: Do nothing (lightkube.core.exceptions.ApiError will be raised)
        lightkube_client: Instantiated lightkube client or None
        logger (Logger): (optional) logger to write log messages to
    """
    _validate_if_exists(if_exists)

    if lightkube_client is None:
        lightkube_client = lightkube.Client()

    for obj in objs:
        try:
            if logger:
                logger.info(f"Creating {obj.metadata.name}.")
            lightkube_client.create(obj)
        except lightkube.core.exceptions.ApiError as e:
            if e.status.code != 409 or if_exists is None:
                raise e
            else:
                if logger:
                    logger.info(
                        f"Caught {e.status} when creating {obj.metadata.name} ({obj.metadata}).  "
                        f"Trying to {if_exists}"
                    )
                if if_exists == "replace":
                    raise NotImplementedError()
                    # Not sure what is wrong with this syntax but it wouldn't work
                elif if_exists == "patch":
                    lightkube_client.patch(
                        type(obj),
                        obj.metadata.name,
                        obj.to_dict(),
                        patch_type=lightkube.types.PatchType.MERGE,
                    )
                else:
                    raise ValueError(
                        "This should not be reached.  This is likely due to an "
                        "uncaught, invalid value for if_exists"
                    )


class CheckFailed(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=None):
        super().__init__()

        self.msg = msg
        self.status_type = status_type
        self.status = status_type(msg)


if __name__ == "__main__":
    main(MetacontrollerOperatorCharm)
