variable "harbor_url" {
  description = "Harbor server URL"
  type        = string
  default     = "https://registry.allegedly.works"
}

variable "authentik_url" {
  description = "Authentik server URL (for OIDC endpoint)"
  type        = string
}
