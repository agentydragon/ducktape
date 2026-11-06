variable "namespace" {
  description = "Namespace containing the CDviz webhook signing Secret."
  type        = string
}

variable "webhook_secret_name" {
  description = "Name of the CDviz webhook signing Secret."
  type        = string
}

variable "webhook_url" {
  description = "HTTPS endpoint that receives GitHub webhook payloads."
  type        = string

  validation {
    condition     = startswith(var.webhook_url, "https://")
    error_message = "The CDviz GitHub webhook URL must use HTTPS."
  }
}
