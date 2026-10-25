variable "forgejo_url" {
  description = "Forgejo internal cluster API URL"
  type        = string
  default     = "http://forgejo-http.forgejo:3000"
}

variable "human_ssh_keys" {
  description = "Optional SSH public keys attached to the managed Authentik human account, keyed by GitLab title."
  type = map(object({
    key        = string
    expires_at = optional(string)
  }))
  default = {}
}

variable "project_deploy_keys" {
  description = "Optional project SSH deploy keys keyed by GitLab title."
  type = map(object({
    key        = string
    can_push   = optional(bool, false)
    expires_at = optional(string)
  }))
  default = {}
}
