# Cluster configuration (from infrastructure)
variable "cluster_name" {
  description = "Name of the Talos cluster"
  type        = string
  default     = "talos-cluster"
}

variable "cluster_domain" {
  description = "Cluster domain name"
  type        = string
  default     = "allegedly.works"
}

variable "talos_version" {
  description = "Talos version for the cluster"
  type        = string
  default     = "v1.12.3"
}

# CLEANUP(2026-03-26): Remove once kernel 6.18 KVM+AMD stall bug is fixed upstream.
#   Talos v1.12 (kernel 6.18) causes periodic CPU stalls on AMD KVM hosts.
#   See debug/wyrm2-chrome-network-changed.md and Red Hat Bugzilla #2448303.
variable "proxmox_talos_version" {
  description = "Talos version for Proxmox nodes (downgraded due to kernel 6.18 AMD KVM bug)"
  type        = string
  default     = "v1.12.3"
}

variable "kubernetes_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.35.1"
}

# Proxmox (shared by persistent-auth + infrastructure + VMs)
variable "proxmox_api_host" {
  description = "Proxmox API host — VLAN IP so CSI pods can reach it"
  type        = string
  # TEMP DISABLED: atlas down. Sinkhole to 127.0.0.1 makes proxmox provider
  # refresh fail immediately ("connection refused") instead of timing out for
  # minutes per resource. Restore to "10.2.0.2" when atlas is back up.
  default = "127.0.0.1"
}

variable "proxmox_node_name" {
  description = "Proxmox node name for VM deployment"
  type        = string
  default     = "atlas"
}

# VM configuration (from nixos-dev-env)
variable "storage" {
  description = "Proxmox storage for VM disks"
  type        = string
  default     = "local-zfs"
}

variable "rebuild_image" {
  description = "Rebuild NixOS bootstrap image (wyrm2)"
  type        = bool
  default     = false
}

variable "nixos_rebuild" {
  description = "Run nixos-rebuild on wyrm2 after apply"
  type        = bool
  default     = false
}

variable "kimsufi_service_name" {
  description = "OVH service name of the first Kimsufi KS-5 server"
  type        = string
  default     = "ns103656.ip-147-135-39.us"
}

variable "kimsufi_service_name_1" {
  description = "OVH service name of the second Kimsufi KS-5 server (empty = not yet provisioned)"
  type        = string
  default     = "ns103711.ip-147-135-39.us"
}

variable "kimsufi_service_name_cp0" {
  description = "OVH service name of the first Kimsufi KS-5 control plane server"
  type        = string
  default     = "ns102453.ip-147-135-37.us"
}

variable "kimsufi_service_name_ks_game_0" {
  description = "OVH service name of the first Kimsufi KS-GAME worker"
  type        = string
  default     = "ns104952.ip-147-135-104.us"
}

variable "kimsufi_service_name_ks_game_1" {
  description = "OVH service name of the second Kimsufi KS-GAME worker"
  type        = string
  default     = "ns104963.ip-147-135-104.us"
}
