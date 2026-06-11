variable "harbor_url" {
  description = "Harbor internal cluster URL"
  type        = string
  default     = "http://harbor-core.harbor:80"
}

variable "flux_webhook_base_url" {
  description = "Base URL for the Flux webhook receiver (HTTPRoute hostname)"
  type        = string
  default     = "https://flux-webhook.allegedly.works"
}
