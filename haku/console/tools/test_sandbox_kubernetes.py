"""Lifecycle tests for the Kubernetes-backed haku_sandbox client."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from unittest.mock import AsyncMock, Mock, patch

import pytest_bazel
from kubernetes_asyncio.client import ApiException, Configuration

import haku.console.tools.sandbox_kubernetes as sandbox_kubernetes
from haku.console.config import AgentSandboxConfig
from haku.console.tools.sandbox_kubernetes import (
    CLAIM_GROUP,
    CLAIMS_PLURAL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    POOL_LABEL,
    SANDBOXES_PLURAL,
    SANDBOX_GROUP,
    KubernetesAgentSandboxClient,
    KubernetesWebSocketExecRunner,
)
from mcp_infra.exec.models import BaseExecResult, Exited, TruncatedStream

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _settings() -> AgentSandboxConfig:
    return AgentSandboxConfig(
        namespace="haku-sandbox",
        warm_pool="haku-bash",
        container="sandbox",
        reserve_timeout_seconds=10,
        poll_interval_seconds=0.1,
    )


def _claim(*, deadline: datetime, ready: bool = True, expired: bool = False) -> dict:
    condition = {
        "type": "Ready",
        "status": "False" if expired or not ready else "True",
        "reason": "ClaimExpired" if expired else ("SandboxNotReady" if not ready else "Ready"),
        "message": "claim state",
    }
    return {
        "metadata": {
            "name": "hs-k7q2m",
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE, POOL_LABEL: "haku-bash"},
        },
        "spec": {
            "warmPoolRef": {"name": "haku-bash"},
            "lifecycle": {"shutdownPolicy": "Retain", "shutdownTime": deadline.isoformat()},
        },
        "status": {
            "conditions": [condition],
            "sandbox": {"name": "haku-bash-abcde"} if not expired else {},
        },
    }


def _sandbox() -> dict:
    return {
        "metadata": {"name": "haku-bash-abcde", "annotations": {"agents.x-k8s.io/pod-name": "pod-abcde"}},
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]},
    }


def _pod(*, ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(name="sandbox", ready=ready)],
        )
    )


def _client(custom: Mock, core: Mock, runner: Mock) -> KubernetesAgentSandboxClient:
    return KubernetesAgentSandboxClient(
        _settings(),
        api_client=Mock(),
        custom_objects=custom,
        core_v1=core,
        exec_runner=runner,
        now=lambda: NOW,
    )


def _route_custom_get(claim: dict):
    async def get(group: str, version: str, namespace: str, plural: str, name: str):
        assert version == "v1beta1"
        assert namespace == "haku-sandbox"
        if (group, plural, name) == (CLAIM_GROUP, CLAIMS_PLURAL, "hs-k7q2m"):
            return claim
        if (group, plural, name) == (SANDBOX_GROUP, SANDBOXES_PLURAL, "haku-bash-abcde"):
            return _sandbox()
        raise AssertionError((group, plural, name))

    return get


async def test_info_reports_ready_only_when_claim_sandbox_and_pod_are_ready() -> None:
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(
        side_effect=_route_custom_get(_claim(deadline=NOW + timedelta(hours=8)))
    )
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(custom, core, Mock()).info("hs-k7q2m")

    assert info.state == "ready"
    assert info.healthy
    assert info.pod_name == "pod-abcde"
    assert info.expires_at == NOW + timedelta(hours=8)


async def test_info_keeps_expiry_observable_without_reading_deleted_resources() -> None:
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(
        return_value=_claim(deadline=NOW - timedelta(seconds=1), expired=True)
    )
    core = Mock()
    core.read_namespaced_pod = AsyncMock()

    info = await _client(custom, core, Mock()).info("hs-k7q2m")

    assert info.state == "expired"
    assert not info.healthy
    assert info.reason == "ClaimExpired"
    core.read_namespaced_pod.assert_not_awaited()


async def test_info_maps_missing_claim_to_not_found() -> None:
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404, reason="Not Found"))

    info = await _client(custom, Mock(), Mock()).info("hs-k7q2m")

    assert info.state == "not_found"
    assert info.expires_at is None


async def test_reserve_creates_retained_claim_and_waits_for_ready() -> None:
    claim = _claim(deadline=NOW + timedelta(hours=8))
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(return_value=claim)
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_custom_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    handle = await _client(custom, core, Mock()).reserve()

    assert handle == "hs-k7q2m"
    body = custom.create_namespaced_custom_object.await_args.args[4]
    assert body["metadata"]["generateName"] == "hs-"
    assert body["spec"]["warmPoolRef"] == {"name": "haku-bash"}
    assert body["spec"]["lifecycle"]["shutdownPolicy"] == "Retain"


async def test_exec_renews_before_and_after_command() -> None:
    claim = _claim(deadline=NOW + timedelta(hours=1))
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_custom_get(claim))
    custom.patch_namespaced_custom_object = AsyncMock(return_value=claim)
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock(
        return_value=BaseExecResult(exit=Exited(exit_code=0), stdout="ok", stderr="", duration_ms=3)
    )

    result = await _client(custom, core, runner).execute(
        handle="hs-k7q2m", cmd=["bash", "-lc", "echo ok"], max_bytes=1000, timeout_ms=5000
    )

    assert custom.patch_namespaced_custom_object.await_count == 2
    for patch_call in custom.patch_namespaced_custom_object.await_args_list:
        assert patch_call.args[5]["spec"]["lifecycle"]["shutdownTime"].startswith("2026-07-22T20:00:00")
    runner.run.assert_awaited_once_with(
        pod_name="pod-abcde",
        namespace="haku-sandbox",
        container="sandbox",
        cmd=["bash", "-lc", "echo ok"],
        max_bytes=1000,
        timeout_ms=5000,
    )
    assert result.expires_at == NOW + timedelta(hours=8)
    assert result.stdout == "ok"


async def test_websocket_exec_wraps_timeout_and_bounds_each_output_stream() -> None:
    configuration = Configuration()
    messages = [
        SimpleNamespace(type=sandbox_kubernetes.WSMsgType.BINARY, data=b"\x01abcdef"),
        SimpleNamespace(type=sandbox_kubernetes.WSMsgType.BINARY, data=b"\x02error"),
        SimpleNamespace(type=sandbox_kubernetes.WSMsgType.BINARY, data=b'\x03{"status":"Success"}'),
        SimpleNamespace(type=sandbox_kubernetes.WSMsgType.CLOSED),
    ]

    class FakeWebSocket:
        async def __aenter__(self) -> FakeWebSocket:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        async def receive(self) -> SimpleNamespace:
            return messages.pop(0)

    real_parse_error_data = sandbox_kubernetes.WsApiClient.parse_error_data

    class FakeWsApiClient:
        def __init__(self, supplied_configuration: Configuration) -> None:
            assert supplied_configuration is configuration

        async def __aenter__(self) -> FakeWsApiClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        parse_error_data = staticmethod(real_parse_error_data)

    class FakeCoreV1Api:
        last_call: tuple[tuple[object, ...], dict[str, object]] | None = None

        def __init__(self, api_client: object) -> None:
            assert isinstance(api_client, FakeWsApiClient)

        async def connect_get_namespaced_pod_exec(self, *args: object, **kwargs: object) -> FakeWebSocket:
            FakeCoreV1Api.last_call = (args, kwargs)
            return FakeWebSocket()

    with (
        patch.object(sandbox_kubernetes, "WsApiClient", FakeWsApiClient),
        patch.object(sandbox_kubernetes.k8s_client, "CoreV1Api", FakeCoreV1Api),
    ):
        result = await KubernetesWebSocketExecRunner(configuration).run(
            pod_name="pod-abcde",
            namespace="haku-sandbox",
            container="sandbox",
            cmd=["bash", "-lc", "echo ok"],
            max_bytes=3,
            timeout_ms=5000,
        )

    assert FakeCoreV1Api.last_call is not None
    args, kwargs = FakeCoreV1Api.last_call
    assert args == ("pod-abcde", "haku-sandbox")
    assert kwargs["command"] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "5.000s",
        "bash",
        "-lc",
        "echo ok",
    ]
    assert kwargs["container"] == "sandbox"
    assert kwargs["stdin"] is False
    assert result.exit == Exited(exit_code=0)
    assert result.stdout == TruncatedStream(truncated_text="abc", total_bytes=6)
    assert result.stderr == TruncatedStream(truncated_text="err", total_bytes=5)


if __name__ == "__main__":
    pytest_bazel.main()
