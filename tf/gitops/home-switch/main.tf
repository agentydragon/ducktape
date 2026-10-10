# Configuration of the home MikroTik CRS310-8G+2S+ switch, reconciled by tofu-controller from a
# runner pinned to the home LAN (cluster/cdk8s/monitoring/home_switch.py). Bootstrap after a
# factory reset: README.md.

# The tofu password is written by bootstrap.sh (SOPS), the monitoring password minted by ESO, and
# the certificate issued by cert-manager from the cluster CA: cluster/cdk8s/monitoring/home_switch.py.
data "kubernetes_secret_v1" "tofu" {
  metadata {
    name      = "home-switch-tofu-password"
    namespace = "monitoring"
  }
}

data "kubernetes_secret_v1" "monitoring" {
  metadata {
    name      = "home-switch-monitoring"
    namespace = "monitoring"
  }
}

data "kubernetes_secret_v1" "tls" {
  metadata {
    name      = "home-switch-tls"
    namespace = "monitoring"
  }
}

provider "routeros" {
  hosturl  = "apis://${var.switch_address}:8729"
  username = "tofu"
  password = data.kubernetes_secret_v1.tofu.data["password"]
  # The cluster CA bundle, mounted into the runner pod.
  ca_certificate = "/etc/cluster-ca/ca-certificates.crt"
}

resource "routeros_system_identity" "this" {
  name = "CRS310"
}

# Importing is create-only, so each renewal by cert-manager imports a new certificate under a new
# name, moves the services onto it, then deletes the old one.
resource "routeros_system_certificate" "tls" {
  name        = "home-switch-${substr(nonsensitive(sha1(data.kubernetes_secret_v1.tls.data["tls.crt"])), 0, 8)}"
  common_name = "CRS310"
  import {
    cert_file_content = data.kubernetes_secret_v1.tls.data["tls.crt"]
    key_file_content  = data.kubernetes_secret_v1.tls.data["tls.key"]
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "routeros_ip_service" "tls" {
  for_each    = toset(["api-ssl", "www-ssl"])
  numbers     = each.key
  port        = each.key == "api-ssl" ? 8729 : 443
  address     = var.lan_cidr
  certificate = routeros_system_certificate.tls.name
  tls_version = "only-1.2"
  disabled    = false
}

resource "routeros_ip_service" "lan_only" {
  for_each = { ssh = 22, winbox = 8291 }
  numbers  = each.key
  port     = each.value
  address  = var.lan_cidr
  disabled = false
}

# Plaintext management.
resource "routeros_ip_service" "disabled" {
  for_each = { api = 8728, ftp = 21, telnet = 23, www = 80 }
  numbers  = each.key
  port     = each.value
  disabled = true
}

resource "routeros_system_user_group" "monitoring" {
  name   = "monitoring"
  policy = ["api", "read", "!ftp", "!local", "!password", "!policy", "!reboot", "!rest-api", "!romon", "!sensitive", "!sniff", "!ssh", "!telnet", "!test", "!web", "!winbox", "!write"]
}

resource "routeros_system_user" "monitoring" {
  name     = "monitoring"
  group    = routeros_system_user_group.monitoring.name
  address  = var.lan_cidr
  password = data.kubernetes_secret_v1.monitoring.data["password"]
  comment  = "Read-only RouterOS exporter; password minted by ESO, see tf/gitops/home-switch."
}

# Log history on the switch, so a log puller can backfill what happened while it couldn't reach
# the switch. RouterOS keeps only 1000 lines in RAM by default, lost on reboot: keep 10k in RAM
# and a rotating 5 x 10k-line copy on flash (about 5 MB). Severities only, never `debug`, which
# would wear the flash. The built-in rules keep sending info, warning and error to memory.
resource "routeros_system_logging_action" "memory" {
  name         = "memory"
  target       = "memory"
  memory_lines = 10000
}

resource "routeros_system_logging_action" "disk" {
  name                = "disk"
  target              = "disk"
  disk_file_count     = 5
  disk_lines_per_file = 10000
  disk_stop_on_full   = false
}

# A rule's topics must all match, so each severity is its own rule.
resource "routeros_system_logging" "disk" {
  for_each = toset(["critical", "error", "warning", "info"])
  action   = routeros_system_logging_action.disk.name
  topics   = [each.key]
}

# The same severities to the cluster's syslog receiver (alloy-syslog), which ships them to Loki.
resource "routeros_system_logging_action" "syslog" {
  name               = "syslog"
  target             = "remote"
  remote             = var.syslog_address
  remote_port        = var.syslog_port
  remote_protocol    = "udp"
  remote_log_format  = "syslog"
  syslog_time_format = "bsd-syslog"
}

resource "routeros_system_logging" "syslog" {
  for_each = toset(["critical", "error", "warning", "info"])
  action   = routeros_system_logging_action.syslog.name
  topics   = [each.key]
}
