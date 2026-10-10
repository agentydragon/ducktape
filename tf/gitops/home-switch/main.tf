# Configuration of the home MikroTik CRS310-8G+2S+ switch, reconciled by tofu-controller from a
# runner pinned to the home LAN (cluster/cdk8s/monitoring/home_switch.py). Bootstrap after a
# factory reset: README.md.

locals {
  # The switch's DHCP lease. bootstrap.sh takes the same address.
  switch_address = "192.168.1.100"
  # The only network management services and the monitoring user accept connections from.
  lan_cidr = "192.168.1.0/24"
}

# Both minted by ESO in cluster/cdk8s/monitoring/home_switch.py.
data "kubernetes_secret_v1" "tofu" {
  metadata {
    name      = "home-switch-tofu"
    namespace = "monitoring"
  }
}

data "kubernetes_secret_v1" "monitoring" {
  metadata {
    name      = "home-switch-monitoring"
    namespace = "monitoring"
  }
}

provider "routeros" {
  hosturl  = "apis://${local.switch_address}:8729"
  username = "tofu"
  password = data.kubernetes_secret_v1.tofu.data["password"]
  # bootstrap.sh signs the api-ssl certificate on the switch itself, and the endpoint is LAN-only.
  insecure = true
}

resource "routeros_system_identity" "this" {
  name = "CRS310"
}

resource "routeros_ip_service" "tls" {
  for_each    = toset(["api-ssl", "www-ssl"])
  numbers     = each.key
  port        = each.key == "api-ssl" ? 8729 : 443
  address     = local.lan_cidr
  certificate = "api" # created by bootstrap.sh
  tls_version = "only-1.2"
  disabled    = false
}

resource "routeros_ip_service" "lan_only" {
  for_each = { ssh = 22, winbox = 8291 }
  numbers  = each.key
  port     = each.value
  address  = local.lan_cidr
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
  address  = local.lan_cidr
  password = data.kubernetes_secret_v1.monitoring.data["password"]
  comment  = "Read-only RouterOS exporter; password minted by ESO, see tf/gitops/home-switch."
}
