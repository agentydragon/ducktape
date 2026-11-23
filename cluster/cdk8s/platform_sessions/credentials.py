"""Platform Sessions gateway bearer files and the namespaces that receive a copy."""

from cluster.cdk8s.secret_ref import SecretKey, SecretRef

NAMESPACE = "platform-sessions"

BEARER_MIRRORS: dict[str, str | None] = {
    "owner": None,
    "aiquota": "cli-proxy-api",
    "quest-scheduler": "agentplane-staging-egress-credentials",
    "myquest-read": None,
    "thrive-scraper": "thrive-scraper",
    "thrive-read": "agentplane-staging-egress-credentials",
    "chatgpt-read": None,
    "blueshield-read": None,
}


def bearer_key(name: str, *, namespace: str = NAMESPACE) -> SecretKey:
    return SecretRef(namespace=namespace, name=f"platform-sessions-{name}-bearer").key("token")
