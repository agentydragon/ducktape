variable "harbor_url" {
  description = "Harbor internal cluster URL"
  type        = string
  default     = "http://harbor-core.harbor:80"
}

variable "vault_address" {
  description = "Vault address"
  type        = string
}

variable "vault_token" {
  description = "Vault token for authentication"
  type        = string
  sensitive   = true
}
