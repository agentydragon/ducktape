# The value is injected from the BuildBuddy admin Secret into the runner environment.
variable "buildbuddy_api_key" {
  description = "BuildBuddy org-admin key used only to manage the sealed LiteLLM test secret."
  type        = string
  sensitive   = true
}
