# These outputs are published to the haku-cloud-agent-ids Secret (flux-system) by
# the Terraform CR's writeOutputsToSecret — the canonical source consumers read
# from. See cluster/k8s/haku/cloud-agent-tf/README.md.
output "vault_id" {
  description = "Vault ID (vlt_*) holding the haku-k8s static_bearer credential."
  value       = claude-managed-agents_vault.haku_cloud.id
}

output "agent_id" {
  description = "Anthropic-hosted Haku agent ID (agent_*)."
  value       = claude-managed-agents_agent.haku_cloud.id
}

output "environment_id" {
  description = "Cloud environment ID (env_*) the agent runs in."
  value       = claude-managed-agents_environment.haku_cloud.id
}

output "deployment_id" {
  description = "Deployment ID (depl_*); use with `ant beta:deployments run` to trigger a session."
  value       = claude-managed-agents_deployment.haku_cloud.id
}
