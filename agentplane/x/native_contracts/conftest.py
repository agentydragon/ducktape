"""Production service composition without the app or an in-test Kubernetes cluster."""

import pytest

from agentplane.proto import session_pb2
from agentplane.testing.native_service_fixtures import (
    api_server,
    client_token,
    control,
    database_clones,
    database_names,
    dispatcher,
    dispatcher_database_url,
    endpoint,
    gateway_service,
    history_service,
    ingress,
    kubeconfig,
    model,
    postgres_admin_url,
    postgres_container,
    postgres_image,
    sandbox,
    service_database_template,
    service_history_db_url,
    spec,
    workspace,
)

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def harness() -> session_pb2.Harness:
    return session_pb2.HARNESS_CLAUDE
