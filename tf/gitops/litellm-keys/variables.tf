variable "buildbuddy_api_key" {
  description = "BuildBuddy org-admin key used only to remove the old LiteLLM BuildBuddy secret during handoff."
  type        = string
  sensitive   = true
}
