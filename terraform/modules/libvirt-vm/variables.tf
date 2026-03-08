# Libvirt VM Module Variables

variable "vm_name" {
  description = "Name of the VM"
  type        = string
}

variable "vcpus" {
  description = "Number of vCPUs"
  type        = number
  default     = 4
}

variable "memory_mb" {
  description = "Memory in MB"
  type        = number
  default     = 8192
}

variable "disk_size_gb" {
  description = "Disk size in GB"
  type        = number
  default     = 50
}

variable "auto_start" {
  description = "Start VM automatically on host boot"
  type        = bool
  default     = true
}

variable "qcow2_image_path" {
  description = "Local path to the pre-built qcow2 image"
  type        = string
}

variable "cloud_init_user_data" {
  description = "Pre-rendered cloud-init user-data YAML (null = no cloud-init)"
  type        = string
  default     = null
  sensitive   = true
}

variable "storage_pool" {
  description = "Libvirt storage pool name"
  type        = string
  default     = "default"
}

variable "network_name" {
  description = "Libvirt network name"
  type        = string
  default     = "default"
}

variable "uefi_firmware_path" {
  description = "Path to UEFI firmware (OVMF)"
  type        = string
  default     = "/usr/share/OVMF/OVMF_CODE_4M.fd"
}
