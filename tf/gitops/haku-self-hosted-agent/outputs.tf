# Published to the haku-self-hosted-agent-ids Secret (flux-system) by the Terraform
# CR's writeOutputsToSecret — the canonical source consumers read from. The
# haku-worker Deployment's ANTHROPIC_ENVIRONMENT_ID is set from environment_id at
# cutover (recreate-fresh; see cluster/k8s/haku/self-hosted-agent-tf/README.md).
output "environment_id" {
  description = "Self-hosted environment ID (env_*); the worker polls this queue."
  value       = claude-managed-agents_environment.haku_selfhosted.id
}

output "agent_id" {
  description = "Self-hosted Haku agent ID (agent_*)."
  value       = claude-managed-agents_agent.haku_selfhosted.id
}

output "vault_id" {
  description = "Vault ID (vlt_*) holding the tana-ro + gmail-labeling static_bearer credentials."
  value       = claude-managed-agents_vault.haku_selfhosted.id
}

output "deployment_id" {
  description = "Deployment ID (depl_*); use with `ant beta:deployments run` to trigger a session."
  value       = claude-managed-agents_deployment.haku_selfhosted.id
}
