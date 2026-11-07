# Shared TokenReview audience for backend callers of LLM ingress and Action Service.
# Sandbox callers use their SVIDs; the egress proxy does not accept this audience.
WORKLOAD_TOKEN_AUDIENCE = "agentplane-egress"
