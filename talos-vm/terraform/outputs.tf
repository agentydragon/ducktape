output "talosconfig" {
  description = "Talos configuration for talosctl"
  value       = data.talos_client_configuration.this.talos_config
  sensitive   = true
}

output "kubeconfig" {
  description = "Kubernetes configuration for kubectl"
  value       = data.talos_cluster_kubeconfig.this.kubeconfig_raw
  sensitive   = true
}

output "talosconfig_file" {
  description = "Path to talosconfig file"
  value       = local_file.talosconfig.filename
}

output "kubeconfig_file" {
  description = "Path to kubeconfig file"
  value       = local_file.kubeconfig.filename
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint"
  value       = "https://127.0.0.1:6443"
}

output "schematic_id" {
  description = "Talos Image Factory schematic ID"
  value       = talos_image_factory_schematic.this.id
}

output "image_urls" {
  description = "Talos Image Factory URLs"
  value = {
    installer  = data.talos_image_factory_urls.this.urls.installer
    kernel     = data.talos_image_factory_urls.this.urls.kernel
    initramfs  = data.talos_image_factory_urls.this.urls.initramfs
  }
}

output "usage_instructions" {
  description = "Instructions for using the cluster"
  value       = <<-EOT
    Talos cluster '${var.cluster_name}' has been created!

    To interact with the cluster:

    1. Use kubectl:
       export KUBECONFIG=${local_file.kubeconfig.filename}
       kubectl get nodes

    2. Use talosctl:
       export TALOSCONFIG=${local_file.talosconfig.filename}
       talosctl --nodes 127.0.0.1 version

    3. Remove control-plane taint (single-node cluster):
       kubectl taint nodes --all node-role.kubernetes.io/control-plane:NoSchedule-

    4. Deploy a test application:
       kubectl create deployment nginx --image=nginx:alpine
       kubectl expose deployment nginx --port=80 --type=NodePort

    Cluster endpoint: https://127.0.0.1:6443
  EOT
}
