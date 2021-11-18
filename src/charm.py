#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
import time
from typing import Optional

import logging
from pathlib import Path

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, MaintenanceStatus, BlockedStatus

import lightkube
from lightkube import codecs
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.core.exceptions import ApiError


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
        try:
            self._create_rbac()
        except ApiError as e:
            # Handle forbidden error as this likely means we don't have --trust
            if e.status.code == 403:
                self.logger.error(
                    "Received Forbidden (403) error from lightkube when creating required RBAC.  "
                    "This may be due to the charm lacking permissions to create cluster-scoped "
                    "roles and resources.  Charm must be deployed with `--trust`"
                )
                self.unit.status = BlockedStatus(
                    "Cannot create required RBAC.  Charm may not have `--trust`"
                )
                return
            else:
                raise e

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
            self.unit.status = BlockedStatus(
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
        create_all_lightkube_objects(
            objs, if_exists=if_exists, lightkube_client=self.lightkube_client
        )

    def _create_rbac(self, if_exists=None):
        self.logger.info("Applying manifests for RBAC")
        objs = self._render_rbac()
        create_all_lightkube_objects(
            objs, if_exists=if_exists, lightkube_client=self.lightkube_client
        )

    def _create_controller(self, if_exists=None):
        self.logger.info("Applying manifests for controller")
        objs = self._render_controller()
        create_all_lightkube_objects(
            objs, if_exists=if_exists, lightkube_client=self.lightkube_client
        )

    def _render_yaml(self, yaml_filename: [str, Path]):
        """Returns a list of lightkube k8s objects for a yaml file, rendered in charm context"""
        context = {
            "namespace": self.model.name,
            "metacontroller_image": METACONTROLLER_IMAGE,
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

    def _render_all_resources(self):
        """Returns a list of lightkube8s objects for all resources, rendered in charm context"""
        return self._render_rbac() + self._render_crds() + self._render_controller()

    def _check_deployed_resources(self):
        """Check the status of all deployed resources, returning True if ok"""
        expected_resources = self._render_all_resources()
        found_resources = [None] * len(expected_resources)
        for i, resource in enumerate(expected_resources):
            self.logger.info(f"Checking for '{resource.metadata}'")
            try:
                found_resources[i] = get_k8s_obj(resource, self.lightkube_client)
            except lightkube.core.exceptions.ApiError:
                self.logger.info(
                    f"Cannot find k8s object for metadata '{resource.metadata}'"
                )
        found_all_resources = all(found_resources)

        statefulset_ok = validate_statefulsets(found_resources, self.logger)

        return found_all_resources and statefulset_ok

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

    @lightkube_client.setter
    def lightkube_client(self, client):
        self._lightkube_client = client


def validate_statefulsets(objs, logger=None):
    """Returns True if all StatefulSets in objs have the expected number of readyReplicas else False

    Optionally emits a message to logger for any StatefulSets that do not have their desired number
    of replicas
    """
    statefulset_not_ok = False
    for obj in objs:
        if isinstance(obj, StatefulSet):
            readyReplicas = obj.status.readyReplicas
            replicas_expected = obj.spec.replicas
            if readyReplicas != replicas_expected:
                statefulset_not_ok = True
                if logger:
                    logger.info(
                        f"StatefulSet {obj.metadata.name} in namespace "
                        f"{obj.metadata.namespace} has {readyReplicas} readyReplicas, "
                        f"expected {replicas_expected}"
                    )
    return not statefulset_not_ok


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


def get_k8s_obj(obj, client=None):
    if not client:
        client = lightkube.Client()

    return client.get(type(obj), obj.metadata.name, namespace=obj.metadata.namespace)


if __name__ == "__main__":
    main(MetacontrollerOperatorCharm)
