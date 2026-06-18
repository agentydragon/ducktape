# HYBRID INFRASTRUCTURE
# Talos cluster control plane currently runs on OVH Kimsufi bare metal.
# Machine secrets generated fresh per lifecycle (prevents stale discovery from previous clusters)

# ============================================================================
# LOCALS: Shared configuration for all nodes
# ============================================================================

locals {
  # Machine secrets generated locally (fresh per lifecycle)
  machine_secrets      = talos_machine_secrets.cluster.machine_secrets
  client_configuration = talos_machine_secrets.cluster.client_configuration

  # Cluster configuration
  cluster_endpoint = "https://localhost:7445" # KubePrism - avoids circular dependency

  # Node topology - Proxmox Talos nodes.
  # talos-pve-cp-0 is retired; Proxmox Kubernetes capacity is provided by NixOS
  # workers instead.
  proxmox_nodes = {}

  # Proxmox network configuration
  proxmox_gateway = "10.2.0.1"

  # Stable Talos endpoint used for post-bootstrap client configuration reads.
  primary_controlplane_ip     = data.ovh_dedicated_server.kimsufi_cp["kimsufi_cp0"].ip
  kubeconfig_cluster_endpoint = "https://api.${var.cluster_domain}:6443"

  # Total expected node count (for health checks)
  expected_node_count = length(local.proxmox_nodes) + length(local.active_kimsufi_servers) + length(local.active_kimsufi_cp_servers)

  # All controlplane endpoints (for talosconfig).
  all_controlplane_ips = concat(
    [for k, v in local.proxmox_nodes : v.ip if v.type == "controlplane"],
    [for k, v in data.ovh_dedicated_server.kimsufi : v.ip if local.active_kimsufi_servers[k].role == "controlplane"],
    [for k, v in data.ovh_dedicated_server.kimsufi_cp : v.ip],
  )

  # Containerd registry mirrors — pull through Harbor proxy cache.
  # Harbor runs in-cluster, so mirrors are unavailable during early bootstrap.
  # Containerd falls back to upstream endpoints when mirror is unreachable.
  #
  # overridePath: Harbor proxy cache serves images at /v2/<project>/<repo>/...,
  # so the endpoint path must replace containerd's default /v2/ prefix, not be
  # prepended to it. Without overridePath, containerd requests
  # /v2/<project>/v2/<repo>/... (double /v2/) which fails upstream.
  registry_mirrors = {
    "docker.io" = {
      endpoints = [
        "https://registry.allegedly.works/v2/docker-hub-proxy",
        "https://registry-1.docker.io",
      ]
      overridePath = true
    }
    "ghcr.io" = {
      endpoints = [
        "https://registry.allegedly.works/v2/ghcr-proxy",
        "https://ghcr.io",
      ]
      overridePath = true
    }
    "gcr.io" = {
      endpoints = [
        "https://registry.allegedly.works/v2/gcr-proxy",
        "https://gcr.io",
      ]
      overridePath = true
    }
    "quay.io" = {
      endpoints = [
        "https://registry.allegedly.works/v2/quay-proxy",
        "https://quay.io",
      ]
      overridePath = true
    }
    "registry.k8s.io" = {
      endpoints = [
        "https://registry.allegedly.works/v2/registry-k8s-io-proxy",
        "https://registry.k8s.io",
      ]
      overridePath = true
    }
  }

  # AuthenticationConfiguration — supports multiple JWT issuers (headlamp + kubectl MCPs).
  # Talos 1.12 has no dedicated authenticationConfig field (unlike authorizationConfig),
  # so we write this file via machine.files and mount it into the kube-apiserver static pod.
  auth_config_content = yamlencode({
    apiVersion = "apiserver.config.k8s.io/v1beta1"
    kind       = "AuthenticationConfiguration"
    jwt = [
      # Headlamp: existing single-user OIDC (migrated from --oidc-* flags)
      {
        issuer = {
          url       = "https://auth.${var.cluster_domain}/application/o/headlamp/"
          audiences = ["headlamp"]
        }
        claimMappings = {
          username = { claim = "preferred_username", prefix = "oidc:" }
          groups   = { claim = "groups", prefix = "oidc-groups:" }
        }
      },
      # kubectl-passthrough-mcp: passthrough kubectl MCP (caller's own permissions)
      {
        issuer = {
          url       = "https://auth.${var.cluster_domain}/application/o/kubectl-passthrough-mcp/"
          audiences = ["kubectl-passthrough-mcp"]
        }
        claimMappings = {
          username = { claim = "preferred_username", prefix = "oidc-ksbx:" }
          groups   = { claim = "groups", prefix = "oidc-ksbx-groups:" }
        }
      },
      # kubectl-sandbox-mcp: scoped kubectl MCP (token exchange → sandbox group only)
      {
        issuer = {
          url       = "https://auth.${var.cluster_domain}/application/o/kubectl-sandbox-mcp/"
          audiences = ["kubectl-sandbox-mcp"]
        }
        claimMappings = {
          username = { claim = "preferred_username", prefix = "oidc-ksbx:" }
          groups   = { claim = "groups", prefix = "oidc-ksbx-groups:" }
        }
      },
      # kubectl-sandbox-client-credentials: machine-to-machine OIDC for
      # write_kubeconfig.py / authentik-jwt-rotation CronJob. Non-interactive
      # client_credentials grant; Authentik maps known machine principals to
      # their effective groups while kube-apiserver keeps trusting one stable
      # issuer/audience.
      {
        issuer = {
          url       = "https://auth.${var.cluster_domain}/application/o/kubectl-sandbox-client-credentials/"
          audiences = ["kubectl-sandbox-client-credentials"]
        }
        claimMappings = {
          username = { claim = "preferred_username", prefix = "oidc-ksbx:" }
          groups   = { claim = "groups", prefix = "oidc-ksbx-groups:" }
        }
      },
    ]
  })

  # Auth config file for CP nodes — written to /var (Talos restricts `op: create`
  # to /var) and mounted into kube-apiserver via extraVolumes.
  # permissions = 420 (= 0644 octal: owner rw, group r, world r)
  cp_auth_files = [
    {
      content     = local.auth_config_content
      path        = "/var/etc/kubernetes/auth/auth-config.yaml"
      op          = "create"
      permissions = 420
    }
  ]

  # Shared kube-apiserver config for all control plane nodes.
  api_server_config = {
    certSANs = ["api.${var.cluster_domain}"]
    extraArgs = {
      # Structured auth replaces the old --oidc-* flags; supports multiple issuers.
      "authentication-config" = "/etc/kubernetes/auth/auth-config.yaml"
    }
    extraVolumes = [
      {
        hostPath  = "/var/etc/kubernetes/auth"
        mountPath = "/etc/kubernetes/auth"
        readonly  = true
      }
    ]
  }

  # Host ingress firewall for kube-controller-manager/kube-scheduler metrics.
  # Those static pods must bind off loopback for ServiceMonitor scrapes, but the
  # metrics ports should remain unreachable from public node IPs.
  control_plane_metrics_firewall_config = yamlencode({
    apiVersion = "v1alpha1"
    kind       = "NetworkRuleConfig"
    name       = "kube-control-plane-metrics"
    portSelector = {
      protocol = "tcp"
      ports    = [10257, 10259]
    }
    ingress = [
      { subnet = "10.42.0.0/16" },
      { subnet = "10.244.0.0/16" },
    ]
  })

  # Controlplane cluster config — includes etcd, apiServer, and inline
  # manifests that Talos rejects on worker nodes.
  common_cluster_config = {
    allowSchedulingOnControlPlanes = false
    apiServer                      = local.api_server_config
    controllerManager = {
      extraArgs = {
        "bind-address" = "0.0.0.0"
      }
    }
    discovery = { enabled = true }
    etcd      = { advertisedSubnets = ["10.42.0.0/16"] }
    network   = { cni = { name = "none" } }
    proxy     = { disabled = true }
    scheduler = {
      extraArgs = {
        "bind-address" = "0.0.0.0"
      }
    }
    # Talos CCM as inline manifest — removes cloud-provider taint before Flux.
    # Cilium stays as a null_resource helm install because it is large and is
    # easier to manage once the k8s API is reachable.
    inlineManifests = [
      { name = "talos-ccm", contents = data.helm_template.talos_ccm.manifest },
    ]
  }

  # Worker-safe cluster config — no etcd, no apiServer, no inline manifests.
  worker_cluster_config = {
    discovery = { enabled = true }
    network   = { cni = { name = "none" } }
    proxy     = { disabled = true }
  }

  # Shared machine base — networking and feature flags common to all nodes.
  # Does not include nodeLabels or kubelet (those differ per provider/node).
  # Includes kubernetesTalosAPIAccess which is CP-only — use
  # worker_machine_base for worker nodes.
  common_machine_base = {
    network = {
      kubespan = {
        enabled = false
      }
    }
    features = {
      kubePrism = {
        enabled = true
        port    = 7445
      }
      # os:reader: safe read-only access (version, metadata, list files —
      # explicitly excludes reading file contents).
      kubernetesTalosAPIAccess = {
        enabled                     = true
        allowedRoles                = ["os:reader"]
        allowedKubernetesNamespaces = ["kube-system", "claude-sandbox", "openclaw-sandbox"]
      }
    }
    registries = {
      mirrors = local.registry_mirrors
    }
    kubelet = {
      nodeIP = {
        validSubnets = ["10.42.0.0/16"]
      }
    }
  }

  # Worker machine base — no kubernetesTalosAPIAccess (Talos rejects it on
  # workers: "feature Kubernetes Talos API Access can only be enabled on
  # control plane machines").
  worker_machine_base = {
    network = {
      kubespan = {
        enabled = false
      }
    }
    features = {
      kubePrism = {
        enabled = true
        port    = 7445
      }
    }
    registries = {
      mirrors = local.registry_mirrors
    }
    kubelet = {
      nodeIP = {
        validSubnets = ["10.42.0.0/16"]
      }
    }
  }
}

# ============================================================================
# TALOS BOOTSTRAP & KUBECONFIG
# ============================================================================

# Bootstrap etcd. This resource records the already-bootstrapped cluster; the
# endpoint is now an OVH control plane.
resource "talos_machine_bootstrap" "cluster" {
  client_configuration = local.client_configuration
  endpoint             = local.primary_controlplane_ip
  node                 = local.primary_controlplane_ip

  depends_on = [
    talos_machine_configuration_apply.kimsufi,
    talos_machine_configuration_apply.kimsufi_cp,
  ]
}

# Generate kubeconfig
resource "talos_cluster_kubeconfig" "cluster" {
  client_configuration = local.client_configuration
  endpoint             = local.primary_controlplane_ip
  node                 = local.primary_controlplane_ip

  depends_on = [talos_machine_bootstrap.cluster]
}

# Generate talosconfig
data "talos_client_configuration" "cluster" {
  cluster_name         = var.cluster_name
  client_configuration = local.client_configuration
  endpoints            = local.all_controlplane_ips
}

# ============================================================================
# LOCAL FILES
# ============================================================================

# Write kubeconfig to file (patched with real IP for external access)
resource "local_file" "kubeconfig" {
  content = replace(
    talos_cluster_kubeconfig.cluster.kubeconfig_raw,
    "https://localhost:7445",
    local.kubeconfig_cluster_endpoint
  )
  filename = "${path.module}/kubeconfig"
}

# Write talosconfig to file
resource "local_file" "talosconfig" {
  content  = data.talos_client_configuration.cluster.talos_config
  filename = "${path.module}/talosconfig.yml"
}
