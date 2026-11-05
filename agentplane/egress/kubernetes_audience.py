"""The audience accepted by the Kubernetes API server for Sandbox tokens."""

# Production sets this as the API server's service-account issuer and accepted audience. It is an
# audience identifier, not an address; keep it independent of the API server's DNS name.
KUBERNETES_AUDIENCE = "https://localhost:7445"
