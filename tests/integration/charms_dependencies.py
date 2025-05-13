"""Charms dependencies for tests."""

from charmed_kubeflow_chisme.testing import CharmSpec

ADMISSION_WEBHOOK = CharmSpec(charm="admission-webhook", channel="1.10/stable", trust=True)
