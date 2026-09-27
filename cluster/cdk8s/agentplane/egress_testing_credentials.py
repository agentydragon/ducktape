"""agentplane-testing's egress credentials: only the GitHub bot PAT behind `egress.py`'s shared
`github-pat` EgressCredential. Testing reaches no other real account.
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s.agentplane.egress_credentials import (
    EXTERNAL_CREDS_READER,
    EXTERNAL_CREDS_STORE,
    GITHUB_PAT_SECRET,
    credential_external_secret,
)


def add_testing_egress_credentials(scope: Construct, *, credentials_namespace: str) -> None:
    construct = Construct(scope, "testing-egress-credentials")
    ServiceAccount(
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
