# A resumed Terraform CR publishes these outputs to haku-cloud-agent-ids. The
# cluster Secret may currently retain the deleted vault ID; see the parked
# resource notes in cluster/parked/cloud-agent-tf/README.md.
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
