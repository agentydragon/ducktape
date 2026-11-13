"""Explicit ambient access for manual system tests against a chosen live deployment."""

import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import grpc
import pytest
from python.runfiles import Runfiles

from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import AGENT_SERVICE_ACCOUNT, AUDIENCE, Cluster, mint_token
from agentplane.system_tests.driver.dispatcher_transport import bound_caller, forward
from agentplane.system_tests.driver.live_dispatcher_tls import verified_dispatcher_proxy
from agentplane.system_tests.driver.tls import service_root_certificates

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


@pytest.fixture
async def dispatcher(cluster: Cluster, tmp_path: Path) -> AsyncIterator[SessionDispatcherClient]:
    """Use URI-pinned upstream TLS for live traffic whose service cert has only a SPIFFE SAN."""
    caller, _, _ = await bound_caller(cluster, "app")
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text(caller.token)
    token_file.chmod(0o600)
    ca_file = tmp_path / "dispatcher-ca.pem"
    ca_file.write_bytes(await service_root_certificates(cluster))
    binary_path = Runfiles.CreateOrRaise().Rlocation(os.environ["LIVE_DISPATCHER_TLS_PROXY_RLOCATION"])
    if binary_path is None:
        raise RuntimeError("live Dispatcher TLS proxy binary is absent from test runfiles")
    binary = Path(binary_path)

    async with AsyncExitStack() as cleanup:
        target = await cleanup.enter_async_context(forward(cluster))
        endpoint = await cleanup.enter_async_context(verified_dispatcher_proxy(binary, target=target, ca_file=ca_file))
        client = await cleanup.enter_async_context(
            SessionDispatcherClient.connect(
                endpoint.address,
                token_file=token_file,
                request_timeout_s=30,
                channel_credentials=grpc.ssl_channel_credentials(root_certificates=endpoint.root_certificates),
                server_name="localhost",
            )
        )
        yield client
