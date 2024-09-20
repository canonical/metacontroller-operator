output "app_name" {
  value = juju_application.metacontroller_operator.name
}

output "provides" {
  value = {
    grafana_dashboard = "grafana-dashboard",
    metrics_endpoint  = "metrics-endpoint",
  }
}

output "requires" {
  value = {}
}
