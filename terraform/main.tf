resource "juju_application" "metacontroller_operator" {
  charm {
    name     = "metacontroller-operator"
    channel  = var.channel
    revision = var.revision
  }
  config    = var.config
  model     = var.model_name
  name      = var.app_name
  resources = var.resources
  trust     = true
  units     = 1
}