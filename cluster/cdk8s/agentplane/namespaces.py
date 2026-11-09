STAGING_NAMESPACE = "agentplane-staging"


def kubernetes_accounts_namespace(namespace: str) -> str:
    """The namespace of the Kubernetes accounts an Agentplane environment's agents act as."""
    return f"{namespace}-k8s-accounts"
