#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
import subprocess
import time

from jinja2 import Environment, FileSystemLoader
import logging
from pathlib import Path

from kubernetes import client, config
import kubernetes.client.exceptions
from oci_image import OCIImageResource
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, MaintenanceStatus


class MetacontrollerOperatorCharm(CharmBase):
    """Charm the Metacontroller"""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        if not self.unit.is_leader():
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

        self.framework.observe(self.on.noop_pebble_ready, self._noop_pebble_ready)
        self.framework.observe(self.on.install, self._install)
        self.framework.observe(self.on.remove, self._remove)
        self.framework.observe(self.on.update_status, self._update_status)
        # self.framework.observe(self.on.config_changed, self._reconcile)

        self.logger = logging.getLogger(__name__)

        # TODO: Fix file imports and move ./src/files back to ./files
        self._manifests_file_root = None
        self.manifest_file_root = "./src/files/manifests/"
        self.image = OCIImageResource(self, "oci-image")

    def _noop_pebble_ready(self, _):
        self.logger.info("noop_pebble_ready fired")

    def _install(self, event):
        self.logger.info("Installing by instantiating Kubernetes objects")
        self.unit.status = MaintenanceStatus("Instantiating Kubernetes objects")
        _, manifests_str = self._render_manifests()

        self.logger.info("Applying manifests")

        completed_process = subprocess.run(
            ["./kubectl", "apply", "-f-"],
            input=manifests_str.encode("utf-8"),
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
        )
        self.logger.info(
            f"kubectl returned with code '{completed_process.returncode}'"
            f" and output {completed_process.stdout}"
        )

        self.logger.info("Waiting for installed Kubernetes objects to be operational")
        max_attempts = 20
        for attempt in range(max_attempts):
            self.logger.info(f"validation attempt {attempt}/{max_attempts}")
            running = self._check_deployed_resources()

            if running is True:
                self.logger.info("Resources detected as running")
                self.logger.info("Install successful")
                self.unit.status = ActiveStatus()
                return
            else:
                sleeptime = 10
                self.logger.info(f"Sleeping {sleeptime}s")
                time.sleep(sleeptime)
        else:
            self.unit.status = MaintenanceStatus(
                "Some kubernetes resources missing/not ready"
            )
            return

    def _update_status(self, event):
        self.logger.info("Comparing current state to desired state")

        running = self._check_deployed_resources()
        if running is True:
            self.logger.info("Resources are ok.  Unit in ActiveStatus")
            self.unit.status = ActiveStatus()
            return
        else:
            self.logger.info(
                "Resources are missing.  Triggering install to reconcile resources"
            )
            self.unit.status = MaintenanceStatus(
                "Missing kubernetes resources detected - reinstalling"
            )
            self._install(event)
            return

    def _remove(self, _):
        """Remove charm"""
        # Should I set a status or does Juju set one?
        self.logger.info("Removing kubernetes objects")

        _, manifests_str = self._render_manifests()
        completed_process = subprocess.run(
            ["./kubectl", "delete", "-f-"],
            input=manifests_str.encode("utf-8"),
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
        )
        if completed_process.returncode == 0:
            self.logger.info("Kubernetes objects removed using kubectl")
        else:
            self.logger.error(
                f"Unable to remove kubernetes objects - there may be orphaned resources."
                f"  kubectl exited with '{completed_process.stdout}'"
            )

    def _render_manifests(self) -> (list, str):
        # Load and render all templates
        self.logger.info(f"Rendering templates from {self.manifest_file_root}")
        jinja_env = Environment(loader=FileSystemLoader(self.manifest_file_root))
        manifests = []
        for f in self._get_manifest_files():
            self.logger.info(f"Rendering template {f}")
            f = Path(f).relative_to(self.manifest_file_root)
            template = jinja_env.get_template(str(f))
            manifests.append(
                template.render(
                    namespace=self.model.name,
                    image="metacontroller/metacontroller:v0.3.0",
                )
            )

        # Combine templates into a single string
        manifests_str = "\n---\n".join(manifests)

        logging.info(f"rendered manifests_str = {manifests_str}")

        return manifests, manifests_str

    def _check_deployed_resources(self, manifests=None):
        """Check the status of all deployed resources, returning True if ok"""
        # TODO: Add checks for other CRDs/services/etc
        # TODO: ideally check all resources automatically based on the manifest
        if manifests is not None:
            raise NotImplementedError("...")
        # if manifests is None:
        #     manifests = self._render_manifests()

        resource_type = "statefulset"
        name = "metacontroller"
        namespace = self.model.name
        self.logger.info(f"Checking {resource_type} {name} in {namespace}")
        try:
            running = validate_statefulset(name=name, namespace=namespace)
            self.logger.info(f"found statefulset running = {running}")
        except kubernetes.client.exceptions.ApiException:
            self.logger.info(
                "got ApiException when looking for statefulset (likely does not exist)"
            )
            running = False

        return running

    @property
    def manifest_file_root(self):
        return self._manifests_file_root

    @manifest_file_root.setter
    def manifest_file_root(self, value):
        self._manifests_file_root = Path(value)

    def _get_manifest_files(self) -> list:
        """Returns a list of all manifest files"""
        return glob.glob(str(self.manifest_file_root / "*.yaml"))


def init_app_client(app_client=None):
    if app_client is None:
        config.load_incluster_config()
        app_client = client.AppsV1Api()
    return app_client


def validate_statefulset(name, namespace, app_client=None):
    """Returns true if a statefulset has its desired number of ready replicas, else False

    Raises a kubernetes.client.exceptions.ApiException from read_namespaced_stateful_set()
    if the object cannot be found
    """
    app_client = init_app_client(app_client)
    ss = app_client.read_namespaced_stateful_set(name, namespace)
    if ss.status.ready_replicas == ss.spec.replicas:
        return True
    else:
        return False


if __name__ == "__main__":
    main(MetacontrollerOperatorCharm)
