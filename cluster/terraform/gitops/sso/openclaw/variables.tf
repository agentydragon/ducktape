variable "authentik_url" {
  description = "Authentik server URL (internal)"
  type        = string
}

variable "authentik_token" {
  description = "Authentik API token"
  type        = string
  sensitive   = true
}

variable "openclaw_url" {
  description = "OpenClaw external URL"
  type        = string
}
