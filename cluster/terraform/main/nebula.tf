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

  # The TF-managed Nebula nodes, identified by hostname (the cert subject is
  # "<host>.nebula.allegedly.works"). Derived from provider-specific node
  # inventories so those resources and Nebula use the same for_each keys.
  nebula_managed_hosts = keys(merge(
    local.kimsufi_servers,
    local.kimsufi_cp_servers,
    local.home_nodes,
  ))

  # Derived: list of all lighthouse IPs (for non-lighthouse nodes' `lighthouse.hosts`).
  nebula_lighthouse_ips = [
    for name, h in local.nebula_hosts : h.nebula_ip if try(h.lighthouse, false)
  ]

  # Derived: static_host_map for every host with a public endpoint.
  nebula_static_host_map = {
    for name, h in local.nebula_hosts :
    h.nebula_ip => [h.endpoint] if can(h.endpoint)
  }

  # Map host → certificate file name.
  nebula_node_names = {
    for host in local.nebula_managed_hosts :
    host => "${host}.nebula.allegedly.works"
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
    # Keep read_buffer/write_buffer above the small kernel default as harmless headroom,
    # but do not treat this as the single-stream fix. Follow-up measurements showed the
    # direct OVH public path is already slow/lossy/asymmetric, so the main bottleneck is
    # below Nebula. See debug/nebula_inter_node_perf and issue #2917.
    listen = { host = "0.0.0.0", port = 4242, read_buffer = 10485760, write_buffer = 10485760 }
    # 2 UDP-processing routines (nebula default is 1 = single-threaded). Nodes are
    # 4-core/8-thread (Xeon E3-1270 v6 / i7-7700K); 2 doubles packet parallelism while
    # leaving cores for etcd/containerd/workloads. A single flow still hashes to one
    # routine, so this mainly lifts aggregate throughput and cannot fix the measured
    # public-underlay single-flow ceiling.
    routines = 2
    punchy   = { punch = true, respond = true }
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
    for host_name in local.nebula_managed_hosts :
    host_name => merge(local.nebula_common, {
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
    for host_name in local.nebula_managed_hosts :
    host_name => merge(local.nebula_common, {
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

  # Either endpoint may require a smaller inner-packet MTU than the mesh-wide
  # nebula1 MTU. Install exact host routes on Talos mesh nodes, taking the
  # smaller endpoint constraint, so Linux segments/fragments before handing
  # packets to Nebula. This keeps the tun device itself at 1420 for Cilium and
  # leaves all unrelated mesh traffic unchanged.
  #
  # Keep this in the legacy machine.network.interfaces config instead of a
  # route-only LinkConfig, which would disable Talos' default DHCP operators.
  # The explicit dhcp=false applies only to nebula1; physical-interface DHCP
  # remains untouched.
  nebula_destination_mtu_routes = {
    for source_name in local.nebula_managed_hosts :
    source_name => [
      for destination_name, destination in local.nebula_hosts : {
        network = "${destination.nebula_ip}/32"
        mtu = min(
          try(local.nebula_hosts[source_name].destination_mtu, 1420),
          try(destination.destination_mtu, 1420),
        )
      }
      if destination_name != source_name && (
        can(local.nebula_hosts[source_name].destination_mtu) ||
        can(destination.destination_mtu)
      )
    ]
  }

  nebula_destination_mtu_patches = {
    for host_name, routes in local.nebula_destination_mtu_routes :
    host_name => length(routes) == 0 ? [] : [yamlencode({
      machine = {
        network = {
          interfaces = [{
            interface = "nebula1"
            dhcp      = false
            routes    = routes
          }]
        }
      }
    })]
  }

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
    key => concat(
      [local.nebula_extension_config[key]],
      local.nebula_destination_mtu_patches[key],
    )
  }
}

# Drift check: every TF-managed host that has a live endpoint must match the
# roster. Skip if the roster declares no endpoint yet.
check "nebula_mesh_endpoint_drift" {
  assert {
    condition = alltrue([
      for host_name in local.nebula_managed_hosts :
      try(local.nebula_hosts[host_name].endpoint, null) == try(local.nebula_live_endpoints[host_name], null)
      if contains(keys(local.nebula_live_endpoints), host_name)
      && try(local.nebula_hosts[host_name].endpoint, null) != null
    ])
    error_message = format(
      "nebula-mesh.json endpoint drift vs live infrastructure (see cluster/docs/mesh_membership.md):\n%s",
      join("\n", [
        for host_name in local.nebula_managed_hosts :
        format("  %s: json=%s live=%s", host_name,
          try(local.nebula_hosts[host_name].endpoint, "<missing>"),
        try(local.nebula_live_endpoints[host_name], "<missing>"))
        if contains(keys(local.nebula_live_endpoints), host_name)
        && try(local.nebula_hosts[host_name].endpoint, null) != null
        && try(local.nebula_hosts[host_name].endpoint, "") != try(local.nebula_live_endpoints[host_name], "")
      ]),
    )
  }
}
