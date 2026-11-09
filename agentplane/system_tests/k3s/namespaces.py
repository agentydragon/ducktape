"""The k3s stack's namespaces: Agentplane's own, the egress proxy's credentials namespace, and the
namespace of the Kubernetes accounts it may impersonate."""

NAMESPACE = "systest"
CREDENTIALS_NAMESPACE = "systest-egress-credentials"
ACCOUNTS_NAMESPACE = "systest-k8s-accounts"
