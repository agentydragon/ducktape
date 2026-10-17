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

data "sops_file" "nebula_key" {
  source_file = "${path.module}/../../../secrets/nebula/home-switch.sops.key"
  input_type  = "raw"
}

locals {
  storage_dir        = "usb1-part1/nebula"
  container_ip       = "172.31.255.2"
  router_veth_ip     = "172.31.255.1"
  router_veth_cidr   = "172.31.255.1/30"
  container_cidr     = "${local.container_ip}/32"
  management_cidrs   = "${var.lan_cidr},${local.container_cidr}"
  mesh_hosts         = jsondecode(file("${path.module}/../../../nebula-mesh.json")).hosts
  lighthouse_ips     = [for host in values(local.mesh_hosts) : host.nebula_ip if try(host.lighthouse, false)]
  static_host_map    = { for host in values(local.mesh_hosts) : host.nebula_ip => [host.endpoint] if can(host.endpoint) }
  nebula_ca          = file("${path.module}/../../../secrets/nebula/ca.crt")
  nebula_cert        = file("${path.module}/../../../secrets/nebula/home-switch.crt")
  nebula_private_key = data.sops_file.nebula_key.raw
  nebula_config = yamlencode({
    pki = {
      ca   = "/etc/nebula/ca.crt"
      cert = "/etc/nebula/host.crt"
      key  = "/etc/nebula/host.key"
    }
    static_host_map = local.static_host_map
    lighthouse = {
      am_lighthouse = false
      interval      = 10
      hosts         = local.lighthouse_ips
    }
    relay = {
      relays     = local.lighthouse_ips
      use_relays = true
    }
    listen  = { host = "0.0.0.0", port = 4242 }
    punchy  = { punch = true, respond = true }
    logging = { level = "info", format = "json" }
    timers  = { connection_alive_interval = 5, pending_deletion_interval = 10 }
    tun     = { dev = "nebula1", mtu = 1420 }
    firewall = {
      outbound = []
      inbound = [
        for port in ["22", "443", "8729", "8291"] : {
          port  = port
          proto = "tcp"
          host  = "any"
        }
      ]
    }
  })
  nebula_proxy_config = jsonencode({ routeros_address = local.router_veth_ip })
}

resource "routeros_system_identity" "this" {
  name = "CRS310"
}

# Importing is create-only, so each renewal by cert-manager imports a new certificate under a new
# name, moves the services onto it, then deletes the old one. bootstrap.sh imports under this same
# name.
#
# Gotcha: the switch must hold one certificate for its address. RouterOS 7.24 serves the first
# certificate whose SAN matches the address a client connected to, whichever one a service names,
# but signs the handshake with the named certificate's key, so clients fail with "bad signature"
# (ECDSA) or "wrong certificate type" (RSA). A renewal passes through that state within one apply;
# if an apply stops there, bootstrap.sh restores a single certificate.
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
  address     = local.management_cidrs
  certificate = routeros_system_certificate.tls.name
  tls_version = "only-1.2"
  disabled    = false
}

resource "routeros_ip_service" "management" {
  for_each = { ssh = 22, winbox = 8291 }
  numbers  = each.key
  port     = each.value
  address  = local.management_cidrs
  disabled = false
}

# The bootstrap-created full-access account authenticates the ToFu runner. Keep its RouterOS
# address restriction aligned with the service allowlist for LAN and proxy-originated connections.
resource "routeros_system_script" "tofu_management_access" {
  name           = "tofu-nebula-management-access"
  policy         = ["read", "write", "policy"]
  source         = <<-EOT
    :local tofuUser [/user find where name="tofu"];
    :if ([:len $tofuUser] = 0) do={ :error "ToFu user not found"; }
    /user set $tofuUser address="${local.management_cidrs}";
  EOT
  launch_trigger = local.management_cidrs
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

# The SSH MCP private key is held in its own SOPS-encrypted Secret. Tofu installs only
# its public half here. This group gives Agentplane broad configuration access over SSH while
# denying every other login service and RouterOS user management, so its password stays empty.
resource "routeros_system_user_group" "agentplane_ssh" {
  name = "agentplane-ssh"
  policy = [
    "ssh",
    "reboot",
    "read",
    "write",
    "test",
    "sniff",
    "sensitive",
    "!ftp",
    "!local",
    "!password",
    "!policy",
    "!rest-api",
    "!romon",
    "!telnet",
    "!web",
    "!winbox",
    "!api",
  ]
}

resource "routeros_system_user" "agentplane" {
  name     = "agentplane"
  group    = routeros_system_user_group.agentplane_ssh.name
  address  = var.lan_cidr
  password = ""
  comment  = "Agentplane SSH MCP; managed by tf/gitops/home-switch."
}

resource "routeros_system_user_sshkeys" "agentplane" {
  user    = routeros_system_user.agentplane.name
  key     = var.agentplane_ssh_public_key
  comment = "Agentplane SSH MCP home-switch key."
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

# RouterOS's USB volume is provisioned by the physical container/device-mode setup. Keep the
# container image layers, extraction scratch space, root filesystem and identity files there.
resource "routeros_system_script" "nebula_storage" {
  name           = "tofu-nebula-storage"
  policy         = ["read", "write", "policy"]
  source         = <<-EOT
    :foreach path in={"${local.storage_dir}"; "${local.storage_dir}/tmp"; "${local.storage_dir}/layers"; "${local.storage_dir}/root"} do={
      :if ([:len [/file find where name=$path]] = 0) do={ /file add name=$path type=directory }
    }
  EOT
  launch_trigger = "nebula-storage-layout-v1"
}

resource "routeros_file" "nebula_ca" {
  name       = "${local.storage_dir}/ca.crt"
  contents   = local.nebula_ca
  depends_on = [routeros_system_script.nebula_storage]
}

resource "routeros_file" "nebula_cert" {
  name       = "${local.storage_dir}/host.crt"
  contents   = local.nebula_cert
  depends_on = [routeros_system_script.nebula_storage]
}

resource "routeros_file" "nebula_key" {
  name       = "${local.storage_dir}/host.key"
  contents   = local.nebula_private_key
  depends_on = [routeros_system_script.nebula_storage]
}

resource "routeros_file" "nebula_config" {
  name       = "${local.storage_dir}/config.yaml"
  contents   = local.nebula_config
  depends_on = [routeros_system_script.nebula_storage]
}

resource "routeros_file" "nebula_proxy_config" {
  name       = "${local.storage_dir}/proxy.json"
  contents   = local.nebula_proxy_config
  depends_on = [routeros_system_script.nebula_storage]
}

resource "routeros_container_mounts" "nebula_ca" {
  name       = "nebula-ca"
  src        = "${local.storage_dir}/ca.crt"
  dst        = "/etc/nebula/ca.crt"
  depends_on = [routeros_file.nebula_ca]
}

resource "routeros_container_mounts" "nebula_cert" {
  name       = "nebula-cert"
  src        = "${local.storage_dir}/host.crt"
  dst        = "/etc/nebula/host.crt"
  depends_on = [routeros_file.nebula_cert]
}

resource "routeros_container_mounts" "nebula_key" {
  name       = "nebula-key"
  src        = "${local.storage_dir}/host.key"
  dst        = "/etc/nebula/host.key"
  depends_on = [routeros_file.nebula_key]
}

resource "routeros_container_mounts" "nebula_config" {
  name       = "nebula-config"
  src        = "${local.storage_dir}/config.yaml"
  dst        = "/etc/nebula/config.yaml"
  depends_on = [routeros_file.nebula_config]
}

resource "routeros_container_mounts" "nebula_proxy_config" {
  name       = "nebula-proxy-config"
  src        = "${local.storage_dir}/proxy.json"
  dst        = "/etc/nebula/proxy.json"
  depends_on = [routeros_file.nebula_proxy_config]
}

resource "routeros_container_config" "this" {
  registry_url = "https://ghcr.io"
  tmpdir       = "${local.storage_dir}/tmp"
  layer_dir    = "${local.storage_dir}/layers"
  depends_on   = [routeros_system_script.nebula_storage]
}

resource "routeros_interface_veth" "nebula" {
  name    = "nebula-veth"
  address = "${local.container_ip}/30"
  gateway = local.router_veth_ip
  comment = "Nebula container for switch management overlay."
}

resource "routeros_ip_address" "nebula_veth" {
  address   = local.router_veth_cidr
  interface = routeros_interface_veth.nebula.name
  comment   = "Router-side address for the Nebula container veth."
}

# Let the container's TCP proxy reach only management listeners on RouterOS itself. The Nebula
# peer never routes a client packet through to the LAN address.
resource "routeros_ip_firewall_filter" "nebula_management_input" {
  action       = "accept"
  chain        = "input"
  dst_address  = local.router_veth_ip
  dst_port     = "22,443,8729,8291"
  in_interface = routeros_interface_veth.nebula.name
  protocol     = "tcp"
  src_address  = local.container_cidr
  place_before = "0"
  comment      = "Allow only the Nebula proxy to reach switch management services."
}

# The container proxies management TCP to RouterOS itself. Block other forwarding from this
# interface, while retaining the UDP/4242 egress Nebula needs to reach lighthouses and peers.
resource "routeros_ip_firewall_filter" "nebula_forward_drop" {
  action       = "drop"
  chain        = "forward"
  in_interface = routeros_interface_veth.nebula.name
  place_before = "0"
  comment      = "Keep the Nebula container from routing into the home LAN."
}

resource "routeros_ip_firewall_filter" "nebula_udp_egress" {
  action       = "accept"
  chain        = "forward"
  dst_port     = "4242"
  in_interface = routeros_interface_veth.nebula.name
  protocol     = "udp"
  place_before = routeros_ip_firewall_filter.nebula_forward_drop.id
  comment      = "Allow Nebula UDP sessions from the container."
}

# The container initiates Nebula's UDP sessions through the switch. Source-NAT those packets so
# the home gateway can return them without needing a route for the private veth subnet.
resource "routeros_ip_firewall_nat" "nebula_egress" {
  action       = "masquerade"
  chain        = "srcnat"
  src_address  = "${local.container_ip}/32"
  place_before = "0"
  comment      = "NAT Nebula container egress through the switch's home-LAN address."
}

resource "routeros_container" "nebula" {
  remote_image = var.nebula_image
  interface    = routeros_interface_veth.nebula.name
  # RouterOS requires a DNS server for containers to start. Peer endpoints are literal IPs, so
  # Nebula itself does not need to resolve names.
  dns      = var.lan_gateway
  root_dir = "${local.storage_dir}/root"
  mounts = [
    routeros_container_mounts.nebula_ca.name,
    routeros_container_mounts.nebula_cert.name,
    routeros_container_mounts.nebula_key.name,
    routeros_container_mounts.nebula_config.name,
    routeros_container_mounts.nebula_proxy_config.name,
  ]
  memory_high           = "128M"
  auto_restart_interval = "1m"
  comment               = "Nebula peer for home-switch; managed by tf/gitops/home-switch."
  logging               = true
  running               = false
  start_on_boot         = true
  check_certificate     = true

  lifecycle {
    # Set privileged=yes via the managed script below before starting. Let it own running state.
    ignore_changes = [running]
  }
  depends_on = [routeros_container_config.this, routeros_ip_address.nebula_veth]
}

# RouterOS 7.24 supports privileged containers, but routeros ~>1.99.0 has no resource argument for
# it. Keep the container stopped in the provider resource, then use its script launch_trigger to
# grant the privilege and start it after every image/config/key change.
resource "routeros_system_script" "nebula_start" {
  name   = "tofu-nebula-start"
  policy = ["read", "write", "policy", "test"]
  source = <<-EOT
    :local target [/container find where comment="Nebula peer for home-switch; managed by tf/gitops/home-switch."];
    :if ([:len $target] = 0) do={ :error "Nebula container not found"; }
    :if ([/container get $target status] = "running") do={ /container stop $target; }
    :for attempt from=1 to=300 do={
      :if ([/container get $target status] = "stopped") do={ :break; }
      :delay 1s;
    }
    :if ([/container get $target status] != "stopped") do={ :error "Nebula container did not reach stopped state"; }
    /container set $target privileged=yes;
    /container start $target;
  EOT
  launch_trigger = sha256(jsonencode({
    image                 = var.nebula_image
    interface             = routeros_interface_veth.nebula.name
    root_dir              = "${local.storage_dir}/root"
    mounts                = ["nebula-ca", "nebula-cert", "nebula-key", "nebula-config", "nebula-proxy-config"]
    memory                = "128M"
    auto_restart_interval = "1m"
    config                = local.nebula_config
    ca                    = local.nebula_ca
    cert                  = local.nebula_cert
    key                   = local.nebula_private_key
    proxy                 = local.nebula_proxy_config
  }))
  depends_on = [
    routeros_container.nebula,
    routeros_file.nebula_ca,
    routeros_file.nebula_cert,
    routeros_file.nebula_key,
    routeros_file.nebula_config,
    routeros_file.nebula_proxy_config,
    routeros_container_mounts.nebula_ca,
    routeros_container_mounts.nebula_cert,
    routeros_container_mounts.nebula_key,
    routeros_container_mounts.nebula_config,
    routeros_container_mounts.nebula_proxy_config,
    routeros_ip_firewall_nat.nebula_egress,
    routeros_ip_firewall_filter.nebula_management_input,
    routeros_ip_firewall_filter.nebula_udp_egress,
  ]
}
