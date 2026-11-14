"""Explicit ambient access for manual system tests against a chosen live deployment."""

import os
from pathlib import Path

import pytest

from agentplane.system_tests.driver.cluster import Cluster

BASE_URL = "AGENTPLANE_SYSTEM_TEST_URL"
TOKEN = "AGENTPLANE_SYSTEM_TEST_TOKEN"
NAMESPACE = "AGENTPLANE_SYSTEM_TEST_NAMESPACE"

STAGING_URL = "https://agentplane-staging.allegedly.works"
STAGING_NAMESPACE = "agentplane-staging"


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get(BASE_URL, STAGING_URL)


@pytest.fixture(scope="session")
def token() -> str:
    """Require a caller-supplied operator ID token for the live app API."""
    return read_token()


def read_token() -> str:
    if not (supplied := os.environ.get(TOKEN)):
        pytest.fail(f"set {TOKEN} to an operator ID token accepted by the target Agentplane app", pytrace=False)
    return supplied


@pytest.fixture(scope="session")
def cluster() -> Cluster:
    """Explicit live deployment access from the caller's kubeconfig and optional environment."""
    return Cluster(
        kubeconfig=Path(os.environ.get("KUBECONFIG", str(Path.home() / ".kube/config"))),
        namespace=os.environ.get(NAMESPACE, STAGING_NAMESPACE),
        kubectl_command=("kubectl",),
    )
