"""Tests for K8sExecutor — verifies pod spec generation and lifecycle logic."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_bazel
from kubernetes_asyncio.client import (
    ApiException,
    V1ContainerState,
    V1ContainerStateTerminated,
    V1ContainerStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodStatus,
)

from props.orchestration.executor import Exited, TimedOut
from props.orchestration.k8s_executor import K8sExecutor, K8sPodHandle, _extract_exit_code


def _make_pod(phase: str, exit_code: int = 0) -> V1Pod:
    """Create a V1Pod with the given phase and exit code."""
    return V1Pod(
        metadata=V1ObjectMeta(resource_version="42"),
        status=V1PodStatus(
            phase=phase,
            container_statuses=[
                V1ContainerStatus(
                    name="agent",
                    image="test",
                    image_id="test",
                    ready=False,
                    restart_count=0,
                    state=V1ContainerState(terminated=V1ContainerStateTerminated(exit_code=exit_code)),
                )
            ],
        ),
    )


@dataclass
class MockedExecutor:
    executor: K8sExecutor
    core_v1: AsyncMock


@pytest.fixture
def mocked_executor() -> MockedExecutor:
    """K8sExecutor with injected mock k8s API client."""
    mock_v1 = AsyncMock()
    executor = K8sExecutor(api_client=AsyncMock(), core_v1=mock_v1, namespace="test-ns")
    return MockedExecutor(executor=executor, core_v1=mock_v1)


async def test_ensure_image_returns_ref_unchanged():
    """ensure_image is a no-op for k8s — returns the ref as-is."""
    executor = K8sExecutor(api_client=AsyncMock(), core_v1=AsyncMock(), namespace="test-ns")
    result = await executor.ensure_image("registry.example.com/agent@sha256:abc123")
    assert result == "registry.example.com/agent@sha256:abc123"


async def test_run_container_creates_pod(mocked_executor: MockedExecutor):
    """run_container creates a pod with correct name, image, env, and labels."""
    handle = await mocked_executor.executor.run_container(
        name="agent-abc123",
        image_id="registry.example.com/critic@sha256:def456",
        env={"PGUSER": "agent_test", "PGPASSWORD": "secret"},
        labels={"adgn.project": "props", "adgn.agent_run_id": "test-uuid"},
    )

    assert handle.name == "agent-abc123"
    assert isinstance(handle, K8sPodHandle)
    assert handle.namespace == "test-ns"

    # Verify the API was called
    mocked_executor.core_v1.create_namespaced_pod.assert_called_once()
    call_kwargs = mocked_executor.core_v1.create_namespaced_pod.call_args

    # Verify pod spec
    pod = call_kwargs.kwargs["body"]
    assert pod.metadata.name == "agent-abc123"
    assert pod.metadata.namespace == "test-ns"
    assert pod.metadata.labels == {"adgn.project": "props", "adgn.agent_run_id": "test-uuid"}
    assert pod.spec.restart_policy == "Never"
    assert pod.spec.automount_service_account_token is False

    container = pod.spec.containers[0]
    assert container.name == "agent"
    assert container.image == "registry.example.com/critic@sha256:def456"
    env_dict = {e.name: e.value for e in container.env}
    assert env_dict == {"PGUSER": "agent_test", "PGPASSWORD": "secret"}


async def test_run_container_with_image_pull_secret():
    """run_container includes imagePullSecrets when configured."""
    mock_v1 = AsyncMock()
    executor = K8sExecutor(
        api_client=AsyncMock(), core_v1=mock_v1, namespace="test-ns", image_pull_secret="registry-creds"
    )

    await executor.run_container(
        name="agent-test", image_id="registry.example.com/critic@sha256:abc", env={}, labels={}
    )

    pod = mock_v1.create_namespaced_pod.call_args.kwargs["body"]
    assert pod.spec.image_pull_secrets is not None
    assert len(pod.spec.image_pull_secrets) == 1
    assert pod.spec.image_pull_secrets[0].name == "registry-creds"


def _watch_events(monkeypatch: pytest.MonkeyPatch, *streams: list[object]) -> list[dict]:
    requests: list[dict] = []
    remaining = iter(streams)

    class FakeWatch:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, _func, **kwargs):
            requests.append(kwargs)
            return self

        async def __aiter__(self):
            for event in next(remaining):
                if isinstance(event, Exception):
                    raise event
                yield event

    monkeypatch.setattr("props.orchestration.k8s_executor.watch.Watch", FakeWatch)
    return requests


async def test_wait_consumes_kubernetes_watch_stream(mocked_executor: MockedExecutor):
    """Use the real watch decoder to consume a Kubernetes event after the initial read."""
    running = _make_pod("Running")
    terminal = _make_pod("Succeeded")
    mocked_executor.core_v1.read_namespaced_pod = AsyncMock(side_effect=[running, terminal])
    mocked_executor.core_v1.read_namespaced_pod_log = AsyncMock(return_value="done")
    response = SimpleNamespace(
        content=SimpleNamespace(
            readline=AsyncMock(
                return_value=b'{"type":"MODIFIED","object":{"metadata":{"name":"agent-test","resourceVersion":"43"},"status":{"phase":"Succeeded"}}}\n'
            )
        ),
        release=Mock(),
    )

    async def list_pods(**kwargs):
        """:rtype: V1PodList"""
        assert kwargs["resource_version"] == "42"
        assert kwargs["watch"] is True
        return response

    mocked_executor.core_v1.list_namespaced_pod = list_pods
    result = await K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1).wait(
        timeout_seconds=60
    )
    assert result.exit == Exited(exit_code=0)
    response.release.assert_called_once()


async def test_wait_restarts_watch_after_expired_resource_version(
    mocked_executor: MockedExecutor, monkeypatch: pytest.MonkeyPatch
):
    running = _make_pod("Running")
    terminal = _make_pod("Succeeded")
    mocked_executor.core_v1.read_namespaced_pod = AsyncMock(side_effect=[running, running, terminal])
    mocked_executor.core_v1.read_namespaced_pod_log = AsyncMock(return_value="done")
    requests = _watch_events(monkeypatch, [ApiException(status=410)], [{"type": "MODIFIED", "object": terminal}])

    result = await K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1).wait(
        timeout_seconds=60
    )

    assert result.exit == Exited(exit_code=0)
    assert len(requests) == 2
    assert all(
        req["resource_version"] == "42" and req["field_selector"] == "metadata.name=agent-test" for req in requests
    )


async def test_wait_reports_pod_deletion(mocked_executor: MockedExecutor, monkeypatch: pytest.MonkeyPatch):
    running = _make_pod("Running")
    mocked_executor.core_v1.read_namespaced_pod = AsyncMock(return_value=running)
    _watch_events(monkeypatch, [{"type": "DELETED", "object": running}])

    with pytest.raises(ApiException) as error:
        await K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1).wait(
            timeout_seconds=60
        )
    assert error.value.status == 404


async def test_wait_succeeded_pod(mocked_executor: MockedExecutor, monkeypatch: pytest.MonkeyPatch):
    """wait returns exit code 0 for a pod that succeeds."""
    running_pod = _make_pod("Running")
    succeeded_pod = _make_pod("Succeeded", exit_code=0)
    mocked_executor.core_v1.read_namespaced_pod = AsyncMock(side_effect=[running_pod, succeeded_pod])
    _watch_events(monkeypatch, [{"type": "MODIFIED", "object": succeeded_pod}])
    mocked_executor.core_v1.read_namespaced_pod_log = AsyncMock(return_value="agent output here")

    handle = K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1)
    result = await handle.wait(timeout_seconds=60)

    assert result.exit == Exited(exit_code=0)
    assert result.stdout == "agent output here"


async def test_wait_failed_pod(mocked_executor: MockedExecutor):
    """wait returns non-zero exit code for a pod that fails."""
    # First call returns Failed (in poll loop), second for exit code extraction.
    failed_pod = _make_pod("Failed", exit_code=1)
    mocked_executor.core_v1.read_namespaced_pod = AsyncMock(side_effect=[failed_pod, failed_pod])
    mocked_executor.core_v1.read_namespaced_pod_log = AsyncMock(return_value="error output")

    handle = K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1)
    result = await handle.wait(timeout_seconds=60)

    assert result.exit == Exited(exit_code=1)
    assert result.stdout == "error output"


async def test_wait_timeout(mocked_executor: MockedExecutor):
    """wait returns TimedOut when pod doesn't finish in time."""
    running_pod = _make_pod("Running")
    mocked_executor.core_v1.read_namespaced_pod = AsyncMock(return_value=running_pod)

    handle = K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1)
    result = await handle.wait(timeout_seconds=0)

    assert result.exit == TimedOut()


async def test_kill_and_delete(mocked_executor: MockedExecutor):
    """kill_and_delete deletes the pod."""
    handle = K8sPodHandle(name="agent-test", namespace="test-ns", core_v1=mocked_executor.core_v1)
    await handle.kill_and_delete()

    mocked_executor.core_v1.delete_namespaced_pod.assert_called_once_with(
        name="agent-test", namespace="test-ns", grace_period_seconds=0
    )


async def test_extract_exit_code_from_terminated():
    """_extract_exit_code reads from container status terminated state."""
    pod = _make_pod("Failed", exit_code=42)
    assert _extract_exit_code(pod) == 42


async def test_extract_exit_code_succeeded_no_status():
    """_extract_exit_code returns 0 for Succeeded phase even without container status."""
    pod = V1Pod(status=V1PodStatus(phase="Succeeded", container_statuses=[]))
    assert _extract_exit_code(pod) == 0


async def test_close():
    """close() closes the API client."""
    api_client = AsyncMock()
    executor = K8sExecutor(api_client=api_client, core_v1=AsyncMock(), namespace="test-ns")
    await executor.close()
    api_client.close.assert_called_once()


if __name__ == "__main__":
    pytest_bazel.main()
