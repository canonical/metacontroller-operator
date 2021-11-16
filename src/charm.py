#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
import subprocess
import time
from typing import Optional

from jinja2 import Environment, FileSystemLoader
import logging
from pathlib import Path

from kubernetes import client, config
import kubernetes.client.exceptions
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, MaintenanceStatus

import lightkube
from lightkube import codecs


METACONTROLLER_IMAGE = "metacontroller/metacontroller:v0.3.0"


class MetacontrollerOperatorCharm(CharmBase):
    """Charm the Metacontroller"""

    def __init__(self, *args):
        super().__init__(*args)

        if not self.unit.is_leader():
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

        self.framework.observe(self.on.install, self._install)
        self.framework.observe(self.on.remove, self._remove)
        self.framework.observe(self.on.update_status, self._update_status)
        # self.framework.observe(self.on.config_changed, self._reconcile)

        self.logger = logging.getLogger(__name__)

        # TODO: Fix file imports and move ./src/files back to ./files
        self._manifests_file_root = None
        self.manifest_file_root = "./src/files/manifests/"

        self._lightkube_client = None

    def _install(self, event):
        self.logger.info("Installing by instantiating Kubernetes objects")
        self.unit.status = MaintenanceStatus("Instantiating Kubernetes objects")

        # TODO: catch error when this fails due to permissions and set appropriate "we aren't
        #  trusted" blocked status
        # create rbac
        self._create_rbac()

        # Create crds
        self._create_crds()

        # deploy the controller
        self._create_controller()

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
        raise NotImplementedError()

    def _create_crds(self, if_exists=None):
        self.logger.info("Applying manifests for CRDs")
        objs = self._render_crds()
        create_all_lightkube_objects(objs, if_exists=if_exists)

    def _create_rbac(self, if_exists=None):
        self.logger.info("Applying manifests for RBAC")
        objs = self._render_rbac()
        create_all_lightkube_objects(objs, if_exists=if_exists)

    def _create_controller(self, if_exists=None):
        self.logger.info("Applying manifests for controller")
        objs = self._render_controller()
        create_all_lightkube_objects(objs, if_exists=if_exists)

    def _render_yaml(self, yaml_filename: [str, Path]):
        """Returns a list of lightkube k8s objects for a yaml file, rendered in charm context"""
        context = {
            "namespace": self.model.name,
            "image": METACONTROLLER_IMAGE,
        }
        with open(self._manifests_file_root / yaml_filename) as f:
            return codecs.load_all_yaml(f, context=context)

    def _render_crds(self):
        """Returns a list of lightkube k8s objects for the CRDs, rendered in charm context"""
        return self._render_yaml("metacontroller-crds-v1.yaml")

    def _render_rbac(self):
        """Returns a list of lightkube k8s objects for the charm RBAC, rendered in charm context"""
        return self._render_yaml("metacontroller-rbac.yaml")

    def _render_controller(self):
        """Returns a list of lightkube k8s objects for the controller, rendered in charm context"""
        return self._render_yaml("metacontroller.yaml")

    def _render_manifests(self) -> (list, str):
        raise Exception("TODO: Remove this")
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
        # TODO: Use lightkube here
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

    @property
    def lightkube_client(self):
        if not self._lightkube_client:
            self._lightkube_client = lightkube.Client()
        return self._lightkube_client


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


ALLOWED_IF_EXISTS = (None, "replace", "patch")


def _validate_if_exists(if_exists):
    if if_exists in ALLOWED_IF_EXISTS:
        return if_exists
    else:
        raise ValueError(
            f"Invalid value for if_exists '{if_exists}'.  Must be one of {ALLOWED_IF_EXISTS}"
        )


def _safe_load_file_to_text(filename: str):
    """Returns the contents of filename if it is an existing file, else it returns filename"""
    try:
        text = Path(filename).read_text()
    except FileNotFoundError:
        text = filename
    return text


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
            lightkube_client.create(obj)
        except lightkube.core.exceptions.ApiError as e:
            if if_exists is None:
                raise e
            else:
                if logger:
                    logger.info(
                        f"Caught {e.status} when creating {obj.metadata.name}.  "
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


if __name__ == "__main__":
    main(MetacontrollerOperatorCharm)
