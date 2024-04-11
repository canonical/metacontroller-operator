from pathlib import Path

import yaml

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
