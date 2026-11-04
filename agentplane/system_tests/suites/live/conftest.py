"""Explicit ambient access for manual system tests against a chosen live deployment."""

import os
from pathlib import Path

import pytest

from agentplane.system_tests.driver.cluster import AGENT_SERVICE_ACCOUNT, AUDIENCE, Cluster, mint_token

BASE_URL = "AGENTPLANE_SYSTEM_TEST_URL"
TOKEN = "AGENTPLANE_SYSTEM_TEST_TOKEN"
NAMESPACE = "AGENTPLANE_SYSTEM_TEST_NAMESPACE"
SERVICE_ACCOUNT = "AGENTPLANE_SYSTEM_TEST_SERVICE_ACCOUNT"

STAGING_URL = "https://agentplane-staging.allegedly.works"
STAGING_NAMESPACE = "agentplane-staging"


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get(BASE_URL, STAGING_URL)


@pytest.fixture(scope="session")
def token(cluster: Cluster) -> str:
    """The caller's own token if it set one, else one minted from the caller's kubeconfig."""
    if (supplied := os.environ.get(TOKEN)) is not None:
        return supplied
    return mint_token(
        cluster=cluster, service_account=os.environ.get(SERVICE_ACCOUNT, AGENT_SERVICE_ACCOUNT), audience=AUDIENCE
    )


@pytest.fixture(scope="session")
def cluster() -> Cluster:
    """Explicit live deployment access from the caller's kubeconfig and optional environment."""
    return Cluster(
        kubeconfig=Path(os.environ.get("KUBECONFIG", str(Path.home() / ".kube/config"))),
        namespace=os.environ.get(NAMESPACE, STAGING_NAMESPACE),
        kubectl_command=("kubectl",),
    )
