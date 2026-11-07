# Shared TokenReview audience for backend services and non-Sandbox egress callers.
# Sandbox workloads authenticate with their SVIDs instead of projecting this audience.
WORKLOAD_TOKEN_AUDIENCE = "agentplane-egress"
