variable "forgejo_url" {
  description = "Forgejo internal cluster API URL"
  type        = string
  default     = "http://forgejo-http.forgejo:3000"
}

variable "webhook_host" {
  description = "Public host of the Flux Receivers; the host Forgejo's package webhook calls"
  type        = string
}

variable "receiver_name" {
  description = "The Flux Receiver the package webhook triggers; its webhook path derives from its name and namespace"
  type        = string
}

variable "receiver_namespace" {
  description = "Namespace of that Receiver, where the webhook token Secret is written"
  type        = string
}

variable "webhook_token_secret" {
  description = "Name of the Secret holding the webhook token, which the Receiver reads"
  type        = string
}
