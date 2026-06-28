# Nebula Mesh — per-node machine config patches
#
# Host roster (SSOT): ../../../nebula-mesh.json
# Add/remove/re-IP runbook: cluster/docs/mesh_membership.md
#
# This file consumes the roster and produces:
#   - Per-node Nebula configs (ExtensionServiceConfig YAMLs mounted by Talos)
#   - A drift check comparing roster endpoints to live OVH IPs
#
# Cert PKI (CA + node certs) lives in persistent-auth.tf, which also reads the
# roster (filtered to tofu-managed hosts).

locals {
  nebula_ca_cert = local_file.nebula_ca_crt.content

  # Single source of truth for the mesh roster.
  nebula_mesh  = jsondecode(file("${path.module}/../../../nebula-mesh.json"))
  nebula_hosts = local.nebula_mesh.hosts

  # Map TF resource keys → roster host names (cert subject =
  # "<host>.nebula.allegedly.works").
  # CLEANUP(added 2026-06-28): now an identity map — the TF for_each keys were renamed to
  #   the hostnames (this commit), so key == value. Drop this local and replace its sole
  #   consumer (nebula_node_names) with the keys directly. Left in place here only to keep
  #   this rename a pure, state-only refactor (no behavior change); remove in a follow-up.
  nebula_tf_key_to_host = {
    "ovh-ns103656" = "ovh-ns103656"
    "ovh-ns103711" = "ovh-ns103711"
    "ovh-ns104952" = "ovh-ns104952"
    "ovh-ns104963" = "ovh-ns104963"
    "ovh-ns102453" = "ovh-ns102453"
  }

  # Derived: list of all lighthouse IPs (for non-lighthouse nodes' `lighthouse.hosts`).
  nebula_lighthouse_ips = [
    for name, h in local.nebula_hosts : h.nebula_ip if try(h.lighthouse, false)
  ]

  # Derived: static_host_map for every host with a public endpoint.
  nebula_static_host_map = {
    for name, h in local.nebula_hosts :
    h.nebula_ip => [h.endpoint] if can(h.endpoint)
  }

  # Map TF key → certificate file name.
  nebula_node_names = {
    for tf_key, host in local.nebula_tf_key_to_host :
    tf_key => "${host}.nebula.allegedly.works"
  }

  nebula_certs = {
    for key, name in local.nebula_node_names :
    key => {
      cert = data.local_file.nebula_node_crt[name].content
      key  = data.local_sensitive_file.nebula_node_key[name].content
    }
  }

  # Live endpoints reported by OVH data sources, keyed by TF resource key. Used
  # by the drift check below.
  nebula_live_endpoints = merge(
    { for k in keys(data.ovh_dedicated_server.kimsufi) : k => "${data.ovh_dedicated_server.kimsufi[k].ip}:4242" },
    { for k in keys(data.ovh_dedicated_server.kimsufi_cp) : k => "${data.ovh_dedicated_server.kimsufi_cp[k].ip}:4242" },
  )

  # PKI paths — must match mountPath values in extensionServiceConfigs below
  nebula_pki = {
    ca   = "/usr/local/etc/nebula/ca.crt"
    cert = "/usr/local/etc/nebula/host.crt"
    key  = "/usr/local/etc/nebula/host.key"
  }

  # Allow all traffic — CiliumNetworkPolicy handles in-cluster isolation
  nebula_firewall = {
    outbound = [{ port = "any", proto = "any", host = "any" }]
    inbound  = [{ port = "any", proto = "any", host = "any" }]
  }

  # Block Cilium/container interfaces from being advertised as Nebula endpoints.
  # Without this, Nebula may advertise pod CIDR IPs (10.244.x.x) to peers,
  # causing a VXLAN-in-Nebula tunnel loop that overwhelms the tun device.
  # See cluster/debug/2026-04-07-pve-cp0-etcd-partition/
  nebula_local_allow_list = {
    interfaces = {
      "cilium.*" = false
      "lxc.*"    = false
    }
  }

  # Fields shared by all nebula nodes — merged with per-node overrides below
  nebula_common = {
    pki             = local.nebula_pki
    static_host_map = local.nebula_static_host_map
    listen          = { host = "0.0.0.0", port = 4242 }
    punchy          = { punch = true, respond = true }
    # Raised from Nebula's default 1300. nebula1 + 60 (Nebula overhead) = 1480,
    # under the 1500 eno1 underlay; carries Cilium's VXLAN (pod 1370 + 50 = 1420).
    # Full MTU model + layering: cluster/docs/network.md.
    tun     = { dev = "nebula1", mtu = 1420 }
    logging = { level = "info", format = "json" }
    timers = {
      connection_alive_interval = 5
      pending_deletion_interval = 10
    }
    firewall = local.nebula_firewall
  }

  # Per-node Nebula daemon configurations.
  #
  # The lighthouse {} and relay {} stanzas have legitimately different shapes
  # for lighthouses vs clients (e.g. lighthouse-only `serve_dns`, `dns`; client-
  # only `hosts`, `use_relays`). Tofu's ternary requires both branches to share
  # a type, so we build the two flavours as separate comprehensions and merge.
  #
  # Invariant for TF-managed hosts: lighthouse=true ⇔ relay=true. Non-
  # lighthouses use lighthouse-as-relay.
  nebula_configs_lighthouse = {
    for tf_key, host_name in local.nebula_tf_key_to_host :
    tf_key => merge(local.nebula_common, {
      lighthouse = {
        am_lighthouse    = true
        serve_dns        = true
        interval         = 10
        dns              = { host = local.nebula_hosts[host_name].nebula_ip, port = 53 }
        local_allow_list = local.nebula_local_allow_list
      }
      relay = { am_relay = true }
    })
    if try(local.nebula_hosts[host_name].lighthouse, false)
  }

  nebula_configs_client = {
    for tf_key, host_name in local.nebula_tf_key_to_host :
    tf_key => merge(local.nebula_common, {
      lighthouse = {
        am_lighthouse    = false
        interval         = 10
        hosts            = local.nebula_lighthouse_ips
        local_allow_list = local.nebula_local_allow_list
      }
      relay = { relays = local.nebula_lighthouse_ips, use_relays = true }
    })
    if !try(local.nebula_hosts[host_name].lighthouse, false)
  }

  nebula_configs = merge(local.nebula_configs_lighthouse, local.nebula_configs_client)

  # Per-node ExtensionServiceConfig documents (apiVersion: v1alpha1 /
  # kind: ExtensionServiceConfig): mount Nebula certs + config into the extension
  # service's filesystem. The extension runs: nebula -config /usr/local/etc/nebula/config.yml
  nebula_extension_config = {
    for key, _ in local.nebula_node_names :
    key => yamlencode({
      apiVersion = "v1alpha1"
      kind       = "ExtensionServiceConfig"
      name       = "nebula"
      configFiles = [
        {
          mountPath = "/usr/local/etc/nebula/ca.crt"
          content   = local.nebula_ca_cert
        },
        {
          mountPath = "/usr/local/etc/nebula/host.crt"
          content   = local.nebula_certs[key].cert
        },
        {
          mountPath = "/usr/local/etc/nebula/host.key"
          content   = local.nebula_certs[key].key
        },
        {
          mountPath = "/usr/local/etc/nebula/config.yml"
          content   = yamlencode(local.nebula_configs[key])
        },
      ]
    })
  }

  # Combined list of patches per node (used in config_patches concat).
  nebula_machine_patches = {
    for key in keys(local.nebula_node_names) :
    key => [local.nebula_extension_config[key]]
  }
}

# Drift check: every TF-managed host that has a live endpoint must match the
# roster. Skip if the roster declares no endpoint yet.
check "nebula_mesh_endpoint_drift" {
  assert {
    condition = alltrue([
      for tf_key, host_name in local.nebula_tf_key_to_host :
      try(local.nebula_hosts[host_name].endpoint, null) == try(local.nebula_live_endpoints[tf_key], null)
      if contains(keys(local.nebula_live_endpoints), tf_key)
      && try(local.nebula_hosts[host_name].endpoint, null) != null
    ])
    error_message = format(
      "nebula-mesh.json endpoint drift vs live infrastructure (see cluster/docs/mesh_membership.md):\n%s",
      join("\n", [
        for tf_key, host_name in local.nebula_tf_key_to_host :
        format("  %s (tf=%s): json=%s live=%s", host_name, tf_key,
          try(local.nebula_hosts[host_name].endpoint, "<missing>"),
        try(local.nebula_live_endpoints[tf_key], "<missing>"))
        if contains(keys(local.nebula_live_endpoints), tf_key)
        && try(local.nebula_hosts[host_name].endpoint, null) != null
        && try(local.nebula_hosts[host_name].endpoint, "") != try(local.nebula_live_endpoints[tf_key], "")
      ]),
    )
  }
}
