"""The `EgressCredential` plural name and the placeholder derived from a credential's name.

Go API types own CRD generation, and the Go proxy owns parsing and enforcement.
"""

CREDENTIALS_PLURAL = "egresscredentials"

PLACEHOLDER_PREFIX = "agentplane-credential-"


def placeholder_of(credential_name: str) -> str:
    """What a sandbox sends where the credential goes. Derived and never authored, so one placeholder
    means one credential without anything having to check that two spellings agree; the separator is
    `-` and not `:` because a `:` cannot be a `basicUsername` component, which ends at the first one.
    """
    return f"{PLACEHOLDER_PREFIX}{credential_name}"
