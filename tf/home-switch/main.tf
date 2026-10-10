# Configuration of the home MikroTik CRS310-8G+2S+ switch. Applied by hand from the home LAN
# (wyrm2); bootstrap and reflash procedure in README.md.

data "sops_file" "admin" {
  source_file = "${path.module}/../../secrets/shared/home-switch-admin.yaml"
}

# Minted by ESO (cluster/cdk8s/monitoring/home_switch.py).
data "kubernetes_secret_v1" "monitoring" {
  metadata {
    name      = "home-switch-monitoring"
    namespace = "monitoring"
  }
}

provider "routeros" {
  hosturl  = var.hosturl
  username = data.sops_file.admin.data["username"]
  password = data.sops_file.admin.data["password"]
  # The api-ssl certificate is self-signed on the switch and the endpoint is LAN-only.
  insecure = true
}

# No public CA issues for a LAN address, so the switch signs its own.
resource "routeros_system_certificate" "api" {
  name             = "api"
  common_name      = "CRS310"
  subject_alt_name = "IP:192.168.1.100"
  key_usage        = ["digital-signature", "key-encipherment", "tls-server"]
  days_valid       = 3650
  sign {}
}

resource "routeros_system_identity" "this" {
  name = "CRS310"
}

resource "routeros_ip_service" "tls" {
  for_each    = toset(["api-ssl", "www-ssl"])
  numbers     = each.key
  port        = each.key == "api-ssl" ? 8729 : 443
  address     = var.lan_cidr
  certificate = routeros_system_certificate.api.name
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

# Plaintext management. Disabling api cuts off the bootstrap endpoint, so the bootstrap
# apply targets only the TLS services.
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
  comment  = "Read-only RouterOS exporter; password minted by ESO, see tf/home-switch."
}
