"""Native Environment transport preserves target identity and never replays mutations."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call, create_autospec

import grpc
import pytest
import pytest_bazel

from agentplane.environments.v1 import environment_service_pb2
from agentplane.grpc.calls import ServiceError
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_NAME, write_bound_fixture_token

# gazelle:include_dep @pypi//protobuf


def _client(stub: object, token_file: Path) -> EnvironmentServiceClient:
    client = object.__new__(EnvironmentServiceClient)
    client.stub = cast(Any, stub)
    client.token_file = token_file
    return client


async def test_native_fixture_token_is_bound_to_one_ready_pod_and_written_private(tmp_path: Path) -> None:
    cluster = create_autospec(Cluster, instance=True)
    cluster.kubectl.side_effect = [
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": NATIVE_CLIENT_NAME, "uid": "pod-uid"},
                        "spec": {"serviceAccountName": NATIVE_CLIENT_NAME},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ]
            }
        ),
        "short-lived-secret",
    ]
    token_file = tmp_path / "native" / "gateway-token"

    name, uid = await write_bound_fixture_token(cluster, token_file, audience="agentplane-runner-gateway")

    assert (name, uid) == (NATIVE_CLIENT_NAME, "pod-uid")
    assert token_file.read_text() == "short-lived-secret"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert cluster.kubectl.call_args_list == [
        call("get", "pods", "-l", "app.kubernetes.io/name=system-test-native-client", "-o", "json"),
        call(
            "create",
            "token",
            NATIVE_CLIENT_NAME,
            "--audience=agentplane-runner-gateway",
            "--duration=1800s",
            "--bound-object-kind=Pod",
            f"--bound-object-name={NATIVE_CLIENT_NAME}",
            "--bound-object-uid=pod-uid",
        ),
    ]


async def test_uncertain_environment_mutation_is_submitted_once(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("test-token")
    unavailable = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, None, None, "backend unavailable")
    suspend = AsyncMock(side_effect=unavailable)
    client = _client(SimpleNamespace(SuspendEnvironment=suspend), token_file)
    target = identity_pb2.EnvironmentTarget(namespace="test", name="owned", uid="expected-uid")

    with pytest.raises(ServiceError):
        await client.suspend_environment(target)

    suspend.assert_awaited_once()
    suspend_call = suspend.await_args
    assert suspend_call is not None
    assert suspend_call.args[0].target == target


async def test_incomplete_or_replaced_target_never_becomes_the_requested_environment(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("test-token")
    delete = AsyncMock()
    client = _client(SimpleNamespace(DeleteEnvironment=delete), token_file)
    with pytest.raises(ValueError, match="complete EnvironmentService target"):
        await client.delete_environment(identity_pb2.EnvironmentTarget(namespace="test", name="owned"))
    delete.assert_not_awaited()

    replacement = environment_service_pb2.EnvironmentDescriptor(
        target=identity_pb2.EnvironmentTarget(namespace="test", name="owned", uid="replacement-uid")
    )
    get = AsyncMock(return_value=replacement)
    client = _client(SimpleNamespace(GetEnvironment=get), token_file)
    requested = identity_pb2.EnvironmentTarget(namespace="test", name="owned", uid="expected-uid")
    with pytest.raises(RuntimeError, match="different target"):
        await client.get_environment(requested)
    get.assert_awaited_once()
    get_call = get.await_args
    assert get_call is not None
    assert get_call.args[0].target == requested


if __name__ == "__main__":
    pytest_bazel.main()
