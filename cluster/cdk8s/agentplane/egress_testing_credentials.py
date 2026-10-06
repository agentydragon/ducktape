"""Credentials copied into agentplane-testing's isolated egress-credentials namespace."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s.agentplane.egress_credentials import (
    BUILDBUDDY_API_KEY_SECRET,
    EXTERNAL_CREDS_READER,
    EXTERNAL_CREDS_STORE,
    GITHUB_PAT_SECRET,
    credential_external_secret,
    inference_credentials,
)


def add_testing_egress_credentials(scope: Construct, *, credentials_namespace: str) -> None:
    construct = Construct(scope, "testing-egress-credentials")
    reader = ServiceAccount(
        construct, "reader", metadata=ApiObjectMetadata(name=EXTERNAL_CREDS_READER, namespace=credentials_namespace)
    )
    credential_external_secret(
        construct,
        namespace=credentials_namespace,
        target=GITHUB_PAT_SECRET,
        source="github-agentydragon-agent",
        key="token",
        store=EXTERNAL_CREDS_STORE,
    )
    credential_external_secret(
        construct,
        namespace=credentials_namespace,
        target=BUILDBUDDY_API_KEY_SECRET,
        source=BUILDBUDDY_API_KEY_SECRET,
        key="api-key",
        store=EXTERNAL_CREDS_STORE,
    )
    inference_credentials(construct, reader=reader, credentials_namespace=credentials_namespace)
