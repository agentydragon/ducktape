"""Run the production Runtime under guest PID 1 and reach it through real Runner Gateway."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.config import (
    Configuration,
    Endpoint,
    EnvironmentRuntimeControl,
    EnvironmentVariables,
    SVIDEndpoint,
    VMEnvironment,
)
from agentplane.proto import session_pb2
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.system_tests.vm.configuration import RESOURCES
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.x.direct_vm_boot.runtime_guest import (
    authenticated_readiness,
    gateway_address,
    guest_image_bind_mounts,
    runtime_guest_container,
    sandbox_with_runtime_gateway,
    write_guest_inputs,
)

# gazelle:include_dep //agentplane/testing:native_gateway_fixtures
pytest_plugins = ("agentplane.testing.native_gateway_fixtures",)


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_with_runtime_gateway(api_server) as started:
        yield started


async def test_packaged_runtime_uses_authenticated_gateway_transport(
    tmp_path: Path,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    control: EnvironmentRuntimeControl,
    gateway_service: GatewayProcess,
    client_token: Path,
) -> None:
    wrong_gateway_client_ca = os.environ.get("DIRECT_VM_BOOT_WRONG_GATEWAY_CLIENT_CA_CONTROL") == "1"
    inputs = tmp_path / "inputs"
    state = tmp_path / "state"
    outputs = tmp_path / "outputs"
    inputs.mkdir()
    state.mkdir()
    outputs.mkdir()
    guest_image_mounts = guest_image_bind_mounts(inputs)

    # No Session is created, so Runtime never delivers through History ingress in this contract.
    unused_ingress = Endpoint(
        target="127.0.0.1:1",
        svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address="127.0.0.1:1"),
    )
    config = Configuration(
        version=2,
        state_dir="/state",
        workspace_root="/state/workspaces",
        initialization_cwd="/state/workspaces",
        control=EnvironmentRuntimeControl(listen_address="0.0.0.0:9090", security=control.security),
        ingress=unused_ingress,
        platform_instructions="Guest Runtime to Gateway transport contract.",
        harness_environment=EnvironmentVariables(
            values={"HOME": "/state/home", "PATH": "/run/current-system/sw/bin:/run/agentplane/environment/bin"}
        ),
        environment=VMEnvironment(
            resources=RESOURCES.model_copy(update={"executor": None}),
            agent_uid=107,
            agent_gid=107,
            block_devices=("/dev/disk/by-id/virtio-workspace",),
        ),
    )
    write_guest_inputs(inputs, sandbox, config, wrong_gateway_client_ca=wrong_gateway_client_ca)
    hostfwd_address = gateway_address(sandbox.uid)
    container_name = f"agentplane-runtime-gateway-{UUID(sandbox.uid).hex[:16]}"

    readiness: asyncio.Task[session_pb2.EnvironmentRuntimeStatus] | None = None
    readiness_error: Exception | None = None
    async with runtime_guest_container(
        container_name=container_name,
        inputs=inputs,
        guest_image_mounts=guest_image_mounts,
        state=state,
        outputs=outputs,
        gateway_address=hostfwd_address,
    ) as docker_done:
        readiness = asyncio.create_task(authenticated_readiness(gateway_service, client_token, api_server, sandbox))
        try:
            done, _ = await asyncio.wait((docker_done, readiness), return_when=asyncio.FIRST_COMPLETED)
            if docker_done in done:
                raise AssertionError("QEMU guest exited before the authenticated Runtime readiness check")
            try:
                await readiness
            except Exception as error:
                readiness_error = error
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10),
                wait=wait_fixed(0.05),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    serial = (outputs / "runtime.serial.log").read_text(errors="replace")
                    assert '"logger":"environment-runtime-lifecycle"' in serial, (
                        "packaged Runtime did not emit lifecycle records before guest teardown"
                    )
        finally:
            if not readiness.done():
                readiness.cancel()
                await asyncio.gather(readiness, return_exceptions=True)

    assert readiness is not None

    serial = (outputs / "runtime.serial.log").read_text(errors="replace")
    runtime_records = [line for line in serial.splitlines() if '"logger":"environment-runtime-lifecycle"' in line]
    assert runtime_records, "packaged Runtime did not emit lifecycle records on the guest serial console"
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((hostfwd_address, 9090), timeout=0.2)

    if wrong_gateway_client_ca:
        gateway_log = gateway_service.log_path.read_text(errors="replace").lower()
        assert "remote error: tls: bad certificate" in gateway_log
    assert readiness_error is None, f"Runtime/Gateway readiness failed: {readiness_error!r}"
    status = readiness.result()
    assert status.environment_runtime_boot_id


if __name__ == "__main__":
    pytest_bazel.main()
