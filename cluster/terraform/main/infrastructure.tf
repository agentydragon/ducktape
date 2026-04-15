# HYBRID INFRASTRUCTURE
# 3-node Talos cluster: 2x Hetzner VPS (controlplane) + 1x Proxmox home (controlplane)
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

  # Node topology - VPS nodes
  vps_nodes = {
    vps0        = { name = "talos-vps-cp-0", server_type = "cpx31", role = "controlplane" }
    vps1        = { name = "talos-vps-cp-1", server_type = "cpx31", role = "controlplane" }
    vps_worker0 = { name = "talos-vps-worker-0", server_type = "cpx31", role = "worker" }
    vps_worker1 = { name = "talos-vps-worker-1", server_type = "cpx31", role = "worker" }
  }

  # Derived: split by role for machine config generation
  vps_cp_nodes     = { for k, v in local.vps_nodes : k => v if v.role == "controlplane" }
  vps_worker_nodes = { for k, v in local.vps_nodes : k => v if v.role == "worker" }

  # Node topology - Proxmox nodes
  # Using VM IDs 10000+ to avoid conflicts with existing cluster (1500-2002)
  proxmox_nodes = {
    pve_cp0 = { name = "talos-pve-cp-0", type = "controlplane", vm_id = 10000, ip = "10.2.1.1" }
  }

  # Proxmox network configuration
  proxmox_gateway = "10.2.0.1"

  # Bootstrap from first VPS (has public IP, most reliable for initial bootstrap)
  bootstrap_node = "vps0"

  # Total expected node count (for health checks)
  expected_node_count = length(local.vps_nodes) + length(local.proxmox_nodes)

  # All controlplane endpoints (for talosconfig) - VPS CP IPs + Proxmox controlplane IPs
  all_controlplane_ips = concat(
    [for k, v in hcloud_server.vps : v.ipv4_address if local.vps_nodes[k].role == "controlplane"],
    [for k, v in local.proxmox_nodes : v.ip if v.type == "controlplane"]
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
      # kubectl-sandbox-mcp: sandbox-scoped kubectl MCP server
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
    ]
  })

  # Shared kube-apiserver config for all control plane nodes (VPS + Proxmox).
  # Centralised here to avoid duplicating between hetzner-nodes.tf and proxmox-nodes.tf.
  api_server_config = {
    certSANs = ["api.${var.cluster_domain}"]
    extraArgs = {
      # Structured auth replaces the old --oidc-* flags; supports multiple issuers.
      "authentication-config" = "/etc/kubernetes/auth/auth-config.yaml"
    }
    extraVolumes = [
      {
        hostPath  = "/etc/kubernetes/auth"
        mountPath = "/etc/kubernetes/auth"
        readOnly  = true
      }
    ]
  }

  # Controlplane cluster config — includes etcd, apiServer, and inline
  # manifests that Talos rejects on worker nodes.
  common_cluster_config = {
    allowSchedulingOnControlPlanes = false
    apiServer                      = local.api_server_config
    discovery                      = { enabled = true }
    etcd                           = { advertisedSubnets = ["10.42.0.0/16"] }
    network                        = { cni = { name = "none" } }
    proxy                          = { disabled = true }
    # Talos CCM as inline manifest — removes cloud-provider taint before Flux.
    # Cilium is too large (~82KB) for Hetzner's 32KB user_data limit, so it
    # stays as a null_resource helm install (see cilium.tf).
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
# HETZNER VPS NODES (see hetzner-nodes.tf for server resources)
# ============================================================================

# SSH key for emergency rescue mode access
resource "tls_private_key" "ssh" {
  algorithm = "ED25519"
}

resource "hcloud_ssh_key" "talos" {
  name       = "talos-cluster"
  public_key = tls_private_key.ssh.public_key_openssh
}

# Firewall for Talos/Kubernetes traffic
resource "hcloud_firewall" "talos" {
  name = "talos-cluster"

  # Kubernetes API
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "6443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Talos API
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "50000"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Talos trustd (cluster join)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "50001"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Nebula mesh overlay
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "4242"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Cilium VXLAN overlay (between nodes)
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "8472"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Cilium health checks (cluster health probes between nodes)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "4240"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # HTTPS ingress
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Docker CI (DinD mTLS)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "2376"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # HTTP (for ACME)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # DNS (TCP)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "53"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # DNS (UDP)
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "53"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # etcd (between controllers)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "2379-2380"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Kubelet API
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "10250"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # ICMP (ping)
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

# ============================================================================
# TALOS BOOTSTRAP & KUBECONFIG
# ============================================================================

# Bootstrap etcd on the first VPS node. All other nodes are already configured
# and waiting in the etcd join retry loop — they join automatically after this.
resource "talos_machine_bootstrap" "cluster" {
  client_configuration = local.client_configuration
  endpoint             = hcloud_server.vps[local.bootstrap_node].ipv4_address
  node                 = hcloud_server.vps[local.bootstrap_node].ipv4_address

  depends_on = [
    talos_machine_configuration_apply.vps,
    talos_machine_configuration_apply.proxmox,
  ]
}

# Generate kubeconfig
resource "talos_cluster_kubeconfig" "cluster" {
  client_configuration = local.client_configuration
  endpoint             = hcloud_server.vps[local.bootstrap_node].ipv4_address
  node                 = hcloud_server.vps[local.bootstrap_node].ipv4_address

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
    "https://${hcloud_server.vps[local.bootstrap_node].ipv4_address}:6443"
  )
  filename = "${path.module}/kubeconfig"
}

# Write talosconfig to file
resource "local_file" "talosconfig" {
  content  = data.talos_client_configuration.cluster.talos_config
  filename = "${path.module}/talosconfig.yml"
}
