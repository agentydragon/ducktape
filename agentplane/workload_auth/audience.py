# Minted into the Sandbox Pod's projected token, verified unchanged by the central egress proxy,
# then forwarded to the LLM ingress and the Action Service, which verify it again: every hop must
# accept this same string.
WORKLOAD_TOKEN_AUDIENCE = "agentplane-egress"
