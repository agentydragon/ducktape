"""Native creation registers exact ownership before readiness and preserves uncertain creates."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import create_autospec

import pytest

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto.identity_pb2 import EnvironmentTarget
from agentplane.system_tests.driver import native_sandbox
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.identity import ScenarioIdentity
from agentplane.system_tests.driver.native_platform import NativePlatformClients

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_native_scope_releases_acknowledged_incarnation_but_retains_uncertain_identity(
    monkeypatch: pytest.MonkeyPatch, acknowledged: bool
) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    target = EnvironmentTarget(namespace="test", name="owned-native", uid="acknowledged-uid")
    original = RuntimeError("readiness failed" if acknowledged else "create outcome unknown")
    released: list[tuple[str, str]] = []

    async def create_identity(_cluster: Cluster, name: str, _policies: list[str]) -> ScenarioIdentity:
        return {"namespace": "test", "name": name, "uid": "identity-uid"}

    async def delete_identity(_cluster: Cluster, identity: ScenarioIdentity) -> None:
        released.append(("identity", identity["uid"]))

    async def delete_sandbox(_cluster: Cluster, name: str, uid: str) -> None:
        assert name == "owned-native"
        released.append(("sandbox", uid))

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield [
            environment_service_pb2.EnvironmentDescriptor(
                target=target,
                observed_state=environment_service_pb2.EnvironmentObservedState(
                    operating_mode=environment_service_pb2.OPERATING_MODE_RUNNING,
                    readiness=environment_service_pb2.Readiness(
                        state=environment_service_pb2.READINESS_STATE_NOT_READY
                    ),
                ),
            )
        ]
        # The acknowledged handle must already have been copied into the owner.
        target.uid = "replacement-uid"
        raise original

    monkeypatch.setattr(native_sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(native_sandbox, "delete_scenario_identity", delete_identity)
    monkeypatch.setattr(native_sandbox, "delete_sandbox", delete_sandbox)
    platform.environment.create_environment.return_value = target
    platform.environment.environment_snapshots.side_effect = snapshots
    if not acknowledged:
        platform.environment.create_environment.side_effect = original

    with pytest.raises(RuntimeError) as raised:
        async with native_sandbox.native_sandbox_scope(cluster, cast(NativePlatformClients, platform)) as create:
            await create("owned-native")
    assert raised.value is original
    assert released == ([("sandbox", "acknowledged-uid"), ("identity", "identity-uid")] if acknowledged else [])


async def test_native_scope_retains_identity_when_sandbox_cleanup_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    target = EnvironmentTarget(namespace="test", name="owned-native", uid="acknowledged-uid")
    identity: ScenarioIdentity = {"namespace": "test", "name": "owned-native", "uid": "identity-uid"}
    identity_deletions: list[str] = []

    async def create_identity(_cluster: Cluster, _name: str, _policies: list[str]) -> ScenarioIdentity:
        return identity

    async def fail_delete(_cluster: Cluster, _name: str, _uid: str) -> None:
        raise RuntimeError("Sandbox deletion was not confirmed")

    original = RuntimeError("runner was not ready")

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield [
            environment_service_pb2.EnvironmentDescriptor(
                target=target,
                observed_state=environment_service_pb2.EnvironmentObservedState(
                    operating_mode=environment_service_pb2.OPERATING_MODE_RUNNING,
                    readiness=environment_service_pb2.Readiness(state=environment_service_pb2.READINESS_STATE_READY),
                ),
            )
        ]

    async def delete_identity(_cluster: Cluster, deleted: ScenarioIdentity) -> None:
        identity_deletions.append(deleted["uid"])

    monkeypatch.setattr(native_sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(native_sandbox, "delete_scenario_identity", delete_identity)
    monkeypatch.setattr(native_sandbox, "delete_sandbox", fail_delete)
    platform.environment.create_environment.return_value = target
    platform.environment.environment_snapshots.side_effect = snapshots
    platform.ready_runner.side_effect = original

    with pytest.raises(RuntimeError, match="runner was not ready") as raised:
        async with native_sandbox.native_sandbox_scope(cluster, cast(NativePlatformClients, platform)) as create:
            await create("owned-native")

    assert raised.value is original
    assert identity_deletions == []


async def test_native_scope_never_releases_a_foreign_namespace_namesake(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    target = EnvironmentTarget(namespace="other", name="same-name", uid="acknowledged-uid")
    deleted: list[tuple[str, str]] = []
    identities: list[str] = []

    async def create_identity(_cluster: Cluster, name: str, _policies: list[str]) -> ScenarioIdentity:
        return {"namespace": "test", "name": name, "uid": "identity-uid"}

    async def delete_identity(_cluster: Cluster, identity: ScenarioIdentity) -> None:
        identities.append(identity["uid"])

    async def delete_sandbox(_cluster: Cluster, name: str, uid: str) -> None:
        deleted.append((name, uid))

    monkeypatch.setattr(native_sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(native_sandbox, "delete_scenario_identity", delete_identity)
    monkeypatch.setattr(native_sandbox, "delete_sandbox", delete_sandbox)
    platform.environment.create_environment.return_value = target

    with pytest.raises(RuntimeError, match="created other/same-name outside this fixture"):
        async with native_sandbox.native_sandbox_scope(cluster, cast(NativePlatformClients, platform)) as create:
            await create("same-name")

    assert deleted == []
    assert identities == []


async def test_wrong_name_acknowledgement_is_owned_before_contract_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    target = EnvironmentTarget(namespace="test", name="unexpected", uid="acknowledged-uid")
    events: list[str] = []

    async def create_identity(_cluster: Cluster, name: str, _policies: list[str]) -> ScenarioIdentity:
        return {"namespace": "test", "name": name, "uid": "identity-uid"}

    async def delete_identity(_cluster: Cluster, _identity: ScenarioIdentity) -> None:
        events.append("identity")

    async def delete_sandbox(_cluster: Cluster, name: str, uid: str) -> None:
        assert (name, uid) == ("unexpected", "acknowledged-uid")
        events.append("sandbox")

    monkeypatch.setattr(native_sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(native_sandbox, "delete_scenario_identity", delete_identity)
    monkeypatch.setattr(native_sandbox, "delete_sandbox", delete_sandbox)
    platform.environment.create_environment.return_value = target

    with pytest.raises(RuntimeError, match="created unexpected name 'unexpected' for 'requested'"):
        async with native_sandbox.native_sandbox_scope(cluster, cast(NativePlatformClients, platform)) as create:
            await create("requested")

    assert events == ["sandbox", "identity"]
    platform.environment.environment_snapshots.assert_not_called()
