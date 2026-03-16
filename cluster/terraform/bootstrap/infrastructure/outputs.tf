# LAYER 1 OUTPUTS - Hybrid Infrastructure
# These outputs are consumed by subsequent layers via terraform_remote_state

# ============================================================================
# KUBECONFIG & ACCESS
# ============================================================================

output "kubeconfig" {
  description = "Generated kubeconfig for cluster access (patched with real endpoint)"
  value = replace(
    talos_cluster_kubeconfig.cluster.kubeconfig_raw,
    "https://localhost:7445",
    "https://${hcloud_server.vps[local.bootstrap_node].ipv4_address}:6443"
  )
  sensitive = true
}

output "kubeconfig_data" {
  description = "Kubeconfig data components for provider configuration"
  value = {
    host                   = "https://${hcloud_server.vps[local.bootstrap_node].ipv4_address}:6443"
    client_certificate     = talos_cluster_kubeconfig.cluster.kubernetes_client_configuration.client_certificate
    client_key             = talos_cluster_kubeconfig.cluster.kubernetes_client_configuration.client_key
    cluster_ca_certificate = talos_cluster_kubeconfig.cluster.kubernetes_client_configuration.ca_certificate
  }
  sensitive = true
}

output "talos_config" {
  description = "Talos client configuration"
  value       = data.talos_client_configuration.cluster.talos_config
  sensitive   = true
}

output "talos_reader_config" {
  description = "Talos readonly client configuration (os:reader role)"
  value       = local.talos_reader_config
  sensitive   = true
}

# ============================================================================
# CLUSTER INFORMATION
# ============================================================================

output "cluster_endpoint" {
  description = "Kubernetes API cluster endpoint"
  value       = "https://${hcloud_server.vps[local.bootstrap_node].ipv4_address}:6443"
}

output "cluster_domain" {
  description = "Cluster domain name for service configuration"
  value       = var.cluster_domain
}

output "cluster_nodes" {
  description = "Cluster node information"
  value = {
    vps_ips = { for k, v in hcloud_server.vps : k => v.ipv4_address }
    # home_ip will be added when Proxmox node is implemented
  }
}

output "vps_node_ips" {
  description = "Public IP addresses of VPS nodes"
  value = {
    for k, v in hcloud_server.vps : k => {
      ipv4 = v.ipv4_address
      ipv6 = v.ipv6_address
    }
  }
}

output "bootstrap_node_ip" {
  description = "IP of the bootstrap node (primary API endpoint)"
  value       = hcloud_server.vps[local.bootstrap_node].ipv4_address
}

# ============================================================================
# INFRASTRUCTURE READINESS
# ============================================================================

output "infrastructure_ready" {
  description = "Indicates infrastructure layer is complete and ready for service deployment"
  sensitive   = true
  value = {
    cluster_ready   = talos_cluster_kubeconfig.cluster.kubeconfig_raw != null
    persistent_auth = data.terraform_remote_state.persistent_auth.outputs.persistent_auth_ready.csi_ready
    timestamp       = timestamp()
  }
}

# Expose controlplane IPs for use by other modules
output "controlplane_ips" {
  description = "List of controlplane node IPs"
  value       = [for k, v in hcloud_server.vps : v.ipv4_address]
}

output "expected_node_count" {
  description = "Expected number of nodes in the cluster"
  value       = local.expected_node_count
}

# ============================================================================
# K8S WORKER JOIN CREDENTIALS (consumed by k8s-worker-proxmox / k8s-worker-libvirt)
# ============================================================================

output "k8s_ca_cert" {
  description = "Kubernetes CA certificate (PEM, base64-encoded)"
  value       = talos_machine_secrets.cluster.machine_secrets.certs.k8s.cert
  sensitive   = true
}

output "k8s_bootstrap_token" {
  description = "Kubernetes bootstrap token for kubelet TLS bootstrap"
  value       = talos_machine_secrets.cluster.machine_secrets.secrets.bootstrap_token
  sensitive   = true
}

output "kubespan_cluster_id" {
  description = "KubeSpan cluster ID for mesh discovery"
  value       = talos_machine_secrets.cluster.machine_secrets.cluster.id
  sensitive   = true
}

output "kubespan_cluster_secret" {
  description = "KubeSpan shared secret for mesh authentication"
  value       = talos_machine_secrets.cluster.machine_secrets.cluster.secret
  sensitive   = true
}
