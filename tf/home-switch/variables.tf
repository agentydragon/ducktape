variable "hosturl" {
  description = "RouterOS API endpoint. Plain api:// only for the bootstrap apply (README), before api-ssl has a certificate."
  type        = string
  default     = "apis://192.168.1.100:8729"
}

variable "lan_cidr" {
  description = "The only network management services and the monitoring user accept connections from."
  type        = string
  default     = "192.168.1.0/24"
}
