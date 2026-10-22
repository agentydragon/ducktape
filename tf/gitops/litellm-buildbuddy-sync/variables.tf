# The value comes from the BuildBuddy admin Secret in the Terraform CR.
variable "buildbuddy_api_key" {
  description = "BuildBuddy org-admin key used only to manage the sealed LiteLLM test secret."
  type        = string
  sensitive   = true
}
