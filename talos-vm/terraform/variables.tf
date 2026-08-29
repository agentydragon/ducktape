variable "cluster_name" {
  description = "Name of the Talos cluster"
  type        = string
  default     = "talos-qemu"
}

variable "talos_version" {
  description = "Talos Linux version"
  type        = string
  default     = "v1.9.2"
}

variable "kubernetes_version" {
  description = "Kubernetes version"
  type        = string
  default     = "v1.32.0"
}

variable "vm_memory" {
  description = "VM memory in MB"
  type        = number
  default     = 2048
}

variable "vm_cpus" {
  description = "Number of VM CPUs"
  type        = number
  default     = 2
}

variable "vm_disk_size" {
  description = "VM disk size in bytes"
  type        = number
  default     = 21474836480 # 20GB
}

variable "proxy_url" {
  description = "HTTP/HTTPS proxy URL for VM"
  type        = string
  default     = "http://10.0.2.2:3128"
}

variable "no_proxy" {
  description = "NO_PROXY environment variable"
  type        = string
  default     = "localhost,127.0.0.1,10.0.2.0/24"
}

variable "dns_servers" {
  description = "List of DNS servers"
  type        = list(string)
  default     = ["10.0.2.3"]
}

variable "insecure_registries" {
  description = "Container registries that should skip TLS verification"
  type        = list(string)
  default     = ["ghcr.io", "gcr.io", "registry.k8s.io", "docker.io"]
}
