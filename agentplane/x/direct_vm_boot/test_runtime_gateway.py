"""Run the production Runtime under guest PID 1 and reach it through real Runner Gateway."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import textwrap
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from python.runfiles import Runfiles

from agentplane.env.config import (
    Configuration,
    Endpoint,
    EnvironmentRuntimeControl,
    EnvironmentVariables,
    SVIDEndpoint,
    VMEnvironment,
)
from agentplane.net.peertls.config import MTLS, TLS
from agentplane.proto import session_pb2
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.system_tests.vm.configuration import RESOURCES
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import make_ca
from agentplane.x.direct_vm_boot import launcher_tools
from devinfra.oci import oci
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep //agentplane/testing:native_gateway_fixtures
pytest_plugins = ("agentplane.testing.native_gateway_fixtures",)


def _runfile(runfiles: Runfiles, name: str) -> Path:
    path = runfiles.Rlocation(os.environ[name])
    if path is None:
        raise FileNotFoundError(f"runfile not found for {name}")
    return Path(path)


def _gateway_address(uid: str) -> str:
    value = UUID(uid).int
    octets = (1 + ((value >> 8) % 126), 1 + ((value >> 16) % 254), 1 + ((value >> 24) % 254))
    return f"127.{octets[0]}.{octets[1]}.{octets[2]}"


async def _set_pod_ip(api_server: KubernetesApiServer, sandbox: SandboxCluster, address: str) -> None:
    core = k8s_client.CoreV1Api(api_server.api)
    await core.patch_namespaced_pod_status(sandbox.name, sandbox.namespace, {"status": {"podIP": address}})
    pod = await core.read_namespaced_pod(sandbox.name, sandbox.namespace)
    assert pod.metadata.uid == sandbox.pod_uid
    assert pod.status.pod_ip == address


def _write_guest_inputs(
    inputs: Path,
    sandbox: SandboxCluster,
    control: EnvironmentRuntimeControl,
    ingress: Endpoint,
    *,
    wrong_gateway_client_ca: bool,
) -> None:
    for directory in ("config", "launch", "runtime-tls", "runtime-trust", "runtime-workload-trust"):
        (inputs / directory).mkdir(parents=True, exist_ok=True)
    (inputs / "config" / "environment-runtime.env").write_text("")

    security = control.security
    assert isinstance(security, MTLS)
    source_tls = security.tls
    assert source_tls.cert_file is not None
    assert source_tls.key_file is not None
    tls_directory = inputs / "runtime-tls"
    shutil.copyfile(source_tls.cert_file, tls_directory / "tls.crt")
    shutil.copyfile(source_tls.key_file, tls_directory / "tls.key")
    (tls_directory / "tls.key").chmod(0o600)

    assert source_tls.ca_file is not None
    ca_bundle = Path(source_tls.ca_file).read_bytes()
    (inputs / "runtime-trust" / "ca-certificates.crt").write_bytes(ca_bundle)
    workload_ca = make_ca("intentional wrong Gateway client trust").cert_pem if wrong_gateway_client_ca else ca_bundle
    (inputs / "runtime-workload-trust" / "ca-certificates.crt").write_bytes(workload_ca)

    guest_security = MTLS(
        tls=TLS(
            cert_file="/run/agentplane/environment-runtime-tls/tls.crt",
            key_file="/run/agentplane/environment-runtime-tls/tls.key",
            ca_file="/run/agentplane/environment-runtime-trust/ca-certificates.crt",
            client_ca_file="/run/agentplane/environment-runtime-workload-trust/ca-certificates.crt",
        )
    )
    config = Configuration(
        version=2,
        state_dir="/state",
        workspace_root="/state/workspaces",
        initialization_cwd="/state/workspaces",
        control=EnvironmentRuntimeControl(listen_address="0.0.0.0:9090", security=guest_security),
        # No Session is created by this contract; History ingress remains configured but unused.
        ingress=ingress,
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
    (inputs / "config" / "environment-runtime.json").write_text(config.model_dump_json(exclude_none=True))
    (inputs / "launch" / "launch.json").write_text(
        json.dumps({"sandbox": sandbox.identity.model_dump(), "bootstrap": None})
    )


async def _authenticated_readiness(
    gateway: GatewayProcess, token_file: Path, api_server: KubernetesApiServer, sandbox: SandboxCluster
) -> session_pb2.EnvironmentRuntimeStatus:
    remote = gateway.gateway_client(token_file, request_timeout_s=2)
    await gateway.wait_for_runner(remote, sandbox.destination, timeout_s=30)
    status = await remote.runner(sandbox.destination).status()
    assert status.environment_runtime_boot_id
    assert status.HasField("lifecycle")
    assert status.lifecycle.desired_mode == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
    assert status.lifecycle.progress_available
    assert status.lifecycle.progress_current
    assert status.lifecycle.stage == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
    assert not status.lifecycle.error
    assert not status.lifecycle.HasField("blocker")
    assert not status.sessions

    lease_name = f"{sandbox.name}-environment-runtime"
    lease = await k8s_client.CoordinationV1Api(api_server.api).read_namespaced_lease(lease_name, sandbox.namespace)
    annotations = lease.metadata.annotations
    assert lease.metadata.owner_references is not None
    assert any(
        owner.uid == sandbox.uid and owner.name == sandbox.name and owner.kind == "Sandbox"
        for owner in lease.metadata.owner_references
    )
    assert annotations["agentplane.allegedly.works/pod-uid"] == sandbox.pod_uid
    assert annotations["agentplane.allegedly.works/last-connected-pod-uid"] == sandbox.pod_uid
    assert annotations["agentplane.allegedly.works/environment-runtime-boot-id"] == status.environment_runtime_boot_id
    assert lease.spec.lease_transitions is not None
    assert lease.spec.lease_transitions > 0
    return status


def _assert_loopback_listener_free(address: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((address, 9090))


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster(api_server) as started:
        # The Gateway observes the Pod IP during startup, so assign QEMU's unique hostfwd address first.
        address = _gateway_address(started.uid)
        await _set_pod_ip(api_server, started, address)
        _assert_loopback_listener_free(address)
        yield started


async def _stop_container(container_name: str) -> tuple[int, str, str]:
    stop = await asyncio.create_subprocess_exec(
        "docker", "stop", "--time", "10", container_name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(15):
            stdout, stderr = await stop.communicate()
    except TimeoutError:
        stop.kill()
        await stop.wait()
        raise AssertionError("stopping the direct Runtime guest exceeded 15 seconds")
    assert stop.returncode is not None
    return stop.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _export_guest_outputs(outputs: Path, artifacts: Path) -> None:
    for path in outputs.iterdir():
        shutil.copyfile(path, artifacts / path.name)


@asynccontextmanager
async def _runtime_guest_container(
    *,
    container_name: str,
    image: str,
    script: str,
    inputs: Path,
    state: Path,
    outputs: Path,
    gateway_address: str,
    state_host_uid: int,
    state_host_gid: int,
    artifacts: Path,
) -> AsyncIterator[asyncio.Task[int]]:
    docker = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--name",
        container_name,
        "--network=host",
        "--volume",
        f"{inputs}:/inputs:ro",
        "--volume",
        f"{state}:/state",
        "--volume",
        f"{outputs}:/outputs",
        "--env",
        f"GATEWAY_ADDRESS={gateway_address}",
        "--env",
        f"STATE_HOST_UID={state_host_uid}",
        "--env",
        f"STATE_HOST_GID={state_host_gid}",
        "--entrypoint=/bin/sh",
        image,
        "-ec",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    docker_done = asyncio.create_task(docker.wait())
    cleanup_failure: str | None = None
    try:
        yield docker_done
    finally:
        stop_stdout = stop_stderr = ""
        if not docker_done.done():
            try:
                stop_status, stop_stdout, stop_stderr = await _stop_container(container_name)
                if stop_status != 0:
                    cleanup_failure = "stopping the named direct Runtime guest container failed"
            except (AssertionError, OSError) as error:
                cleanup_failure = f"stopping the named direct Runtime guest container failed: {type(error).__name__}"
        (artifacts / "runtime-guest-docker-stop.log").write_text(f"stdout:\n{stop_stdout}\nstderr:\n{stop_stderr}")
        try:
            async with asyncio.timeout(20):
                stdout, stderr = await docker.communicate()
        except TimeoutError:
            docker.kill()
            await docker.wait()
            cleanup_failure = "the named direct Runtime guest container did not exit after bounded stop"
            stdout, stderr = await docker.communicate()
        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        expected_owner_marker = f"state_owner_restore=complete uid={state_host_uid} gid={state_host_gid}"
        if expected_owner_marker not in f"{stdout_text}\n{stderr_text}":
            cleanup_failure = cleanup_failure or "the guest did not confirm restoring the state share ownership"
        try:
            restored_state_owner = await asyncio.to_thread(state.stat)
        except OSError as error:
            cleanup_failure = (
                cleanup_failure or f"could not verify restored state share ownership: {type(error).__name__}"
            )
        else:
            if (restored_state_owner.st_uid, restored_state_owner.st_gid) != (state_host_uid, state_host_gid):
                cleanup_failure = cleanup_failure or "the state share root ownership was not restored to the host owner"
        remove = await asyncio.create_subprocess_exec(
            "docker", "rm", "--force", container_name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            async with asyncio.timeout(15):
                remove_stdout, remove_stderr = await remove.communicate()
        except TimeoutError:
            remove.kill()
            await remove.wait()
            cleanup_failure = cleanup_failure or "removing the direct Runtime guest container exceeded 15 seconds"
            remove_stdout, remove_stderr = await remove.communicate()
        (artifacts / "runtime-guest-docker.log").write_text(
            f"exit={docker.returncode}\nstdout:\n{stdout_text}\nstderr:\n{stderr_text}\n"
            f"remove_exit={remove.returncode}\nremove_stdout:\n{remove_stdout.decode(errors='replace')}\n"
            f"remove_stderr:\n{remove_stderr.decode(errors='replace')}"
        )
        if remove.returncode != 0:
            cleanup_failure = cleanup_failure or "removing the direct Runtime guest container failed"
        await asyncio.to_thread(_export_guest_outputs, outputs, artifacts)
        if cleanup_failure is not None:
            raise AssertionError(cleanup_failure)


async def test_packaged_runtime_uses_authenticated_gateway_transport(
    tmp_path: Path,
    runfiles: Runfiles,
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
    for environment_name, filename in (
        ("KERNEL_RLOCATION", "vmlinuz"),
        ("INITRAMFS_RLOCATION", "initramfs.cpio"),
        ("ROOT_IMAGE_RLOCATION", "root.img"),
        ("RUNNER_IMAGE_RLOCATION", "runner.img"),
    ):
        shutil.copyfile(_runfile(runfiles, environment_name), inputs / filename)

    # No Session is created, so Runtime never delivers through History ingress in this contract.
    unused_ingress = Endpoint(
        target="127.0.0.1:1",
        svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address="127.0.0.1:1"),
    )
    _write_guest_inputs(inputs, sandbox, control, unused_ingress, wrong_gateway_client_ca=wrong_gateway_client_ca)
    gateway_address = _gateway_address(sandbox.uid)

    nonce = UUID(sandbox.uid).hex[:16]
    container_name = f"agentplane-runtime-gateway-{nonce}"
    state_owner = state.stat()
    image = oci.load_oci_image(launcher_tools.LAYOUT_RLOCATION)
    script = textwrap.dedent(
        r"""
        set -eu

        qemu=/usr/libexec/qemu-kvm
        virtiofsd=/usr/libexec/virtiofsd
        xorriso=/usr/bin/xorriso
        mkdir -p /tmp/runtime-gateway/sockets /tmp/runtime-gateway/empty /tmp/runtime-gateway/private-tls
        cp -a /inputs/runtime-tls/. /tmp/runtime-gateway/private-tls/
        chmod 0700 /tmp/runtime-gateway/private-tls
        chmod 0600 /tmp/runtime-gateway/private-tls/tls.key

        cleanup() {
            trap - EXIT
            if [ -n "${qemu_pid:-}" ]; then kill "$qemu_pid" 2>/dev/null || true; fi
            if [ -f /tmp/runtime-gateway/virtiofsd.pids ]; then
                while read -r pid; do kill "$pid" 2>/dev/null || true; done </tmp/runtime-gateway/virtiofsd.pids
                while read -r pid; do wait "$pid" 2>/dev/null || true; done </tmp/runtime-gateway/virtiofsd.pids
            fi
            if [ -n "${qemu_pid:-}" ]; then wait "$qemu_pid" 2>/dev/null || true; fi
            if ! timeout --signal=TERM --kill-after=5s 15s chown -R "$STATE_HOST_UID:$STATE_HOST_GID" /state; then
                printf 'state_owner_restore=failed\n' >&2
                exit 1
            fi
            printf 'state_owner_restore=complete uid=%s gid=%s\n' "$STATE_HOST_UID" "$STATE_HOST_GID"
        }
        trap cleanup EXIT

        # KubeVirt's production virtiofsd runs as 107:107; model that guest-visible ownership here.
        chown 107:107 /state
        printf 'state_owner=107:107 (production virtiofsd mapping)\n'
        for volume in config launch trust; do
            source=/tmp/runtime-gateway/empty
            if [ "$volume" = config ]; then source=/inputs/config; fi
            if [ "$volume" = launch ]; then source=/inputs/launch; fi
            "$xorriso" -as mkisofs -o "/tmp/runtime-gateway/$volume.iso" \
                -volid "agentplane-$volume" "$source" >/dev/null 2>&1
        done
        truncate -s 1G /tmp/runtime-gateway/workspace.img

        start_virtiofs() {
            tag=$1
            shared_dir=$2
            socket="/tmp/runtime-gateway/sockets/$tag.sock"
            "$virtiofsd" --socket-path="$socket" --shared-dir="$shared_dir" --sandbox=none \
                >"/outputs/$tag.virtiofsd.log" 2>&1 &
            echo "$!" >>/tmp/runtime-gateway/virtiofsd.pids
        }
        start_virtiofs state /state
        start_virtiofs agentplane-environment-runtime-tls /tmp/runtime-gateway/private-tls
        start_virtiofs agentplane-environment-runtime-trust /inputs/runtime-trust
        start_virtiofs runner-workload-trust /inputs/runtime-workload-trust
        printf 'launcher_stage=waiting_for_virtiofs_sockets\n'
        for tag in state agentplane-environment-runtime-tls agentplane-environment-runtime-trust runner-workload-trust; do
            socket="/tmp/runtime-gateway/sockets/$tag.sock"
            for attempt in $(seq 1 100); do
                if [ -S "$socket" ]; then break; fi
                sleep 0.1
            done
            if [ ! -S "$socket" ]; then
                printf 'missing vhost-user socket: %s\n' "$socket" >&2
                cat "/outputs/$tag.virtiofsd.log" >&2
                exit 1
            fi
            printf 'vhost_user_socket=ready tag=%s\n' "$tag"
        done

        printf 'launcher_stage=starting_qemu\n'
        timeout --signal=TERM --kill-after=10s 120s "$qemu" \
            -machine q35,accel=tcg -cpu max -nodefaults -m 1536M -smp 2 -no-reboot \
            -kernel /inputs/vmlinuz -initrd /inputs/initramfs.cpio \
            -append "console=ttyS0 net.ifnames=0" \
            -display none -monitor none -serial file:/outputs/runtime.serial.log \
            -object memory-backend-memfd,id=mem,size=1536M,share=on \
            -numa node,memdev=mem \
            -netdev "user,id=net0,net=10.0.2.0/24,host=10.0.2.1,dhcpstart=10.0.2.4,hostfwd=tcp:$GATEWAY_ADDRESS:9090-10.0.2.2:9090" \
            -device virtio-net-pci,netdev=net0 \
            -drive file=/inputs/root.img,format=raw,if=none,id=root,readonly=on \
            -device virtio-blk-pci,drive=root,serial=guest-root \
            -drive file=/inputs/runner.img,format=raw,if=none,id=runner,readonly=on \
            -device virtio-blk-pci,drive=runner,serial=agentplane-runner \
            -drive file=/tmp/runtime-gateway/workspace.img,format=raw,if=none,id=workspace \
            -device virtio-blk-pci,drive=workspace,serial=workspace \
            -drive file=/tmp/runtime-gateway/config.iso,format=raw,if=none,id=config,readonly=on \
            -device virtio-blk-pci,drive=config,serial=agentplane-config \
            -drive file=/tmp/runtime-gateway/launch.iso,format=raw,if=none,id=launch,readonly=on \
            -device virtio-blk-pci,drive=launch,serial=agentplane-launch \
            -drive file=/tmp/runtime-gateway/trust.iso,format=raw,if=none,id=trust,readonly=on \
            -device virtio-blk-pci,drive=trust,serial=agentplane-trust \
            -chardev socket,id=state,path=/tmp/runtime-gateway/sockets/state.sock \
            -device vhost-user-fs-pci,chardev=state,tag=state \
            -chardev socket,id=runtime-tls,path=/tmp/runtime-gateway/sockets/agentplane-environment-runtime-tls.sock \
            -device vhost-user-fs-pci,chardev=runtime-tls,tag=agentplane-environment-runtime-tls \
            -chardev socket,id=runtime-trust,path=/tmp/runtime-gateway/sockets/agentplane-environment-runtime-trust.sock \
            -device vhost-user-fs-pci,chardev=runtime-trust,tag=agentplane-environment-runtime-trust \
            -chardev socket,id=workload-trust,path=/tmp/runtime-gateway/sockets/runner-workload-trust.sock \
            -device vhost-user-fs-pci,chardev=workload-trust,tag=runner-workload-trust \
            >/outputs/runtime.qemu.log 2>&1 &
        qemu_pid=$!
        wait "$qemu_pid"
        """
    )

    artifacts = undeclared_outputs_dir()
    readiness: asyncio.Task[session_pb2.EnvironmentRuntimeStatus] | None = None
    readiness_error: Exception | None = None
    async with _runtime_guest_container(
        container_name=container_name,
        image=image,
        script=script,
        inputs=inputs,
        state=state,
        outputs=outputs,
        gateway_address=gateway_address,
        state_host_uid=state_owner.st_uid,
        state_host_gid=state_owner.st_gid,
        artifacts=artifacts,
    ) as docker_done:
        readiness = asyncio.create_task(_authenticated_readiness(gateway_service, client_token, api_server, sandbox))
        try:
            done, _ = await asyncio.wait((docker_done, readiness), return_when=asyncio.FIRST_COMPLETED)
            if docker_done in done:
                raise AssertionError("QEMU guest exited before the authenticated Runtime readiness check")
            try:
                await readiness
            except Exception as error:
                readiness_error = error
        finally:
            if not readiness.done():
                readiness.cancel()
                await asyncio.gather(readiness, return_exceptions=True)

    assert readiness is not None

    serial = (outputs / "runtime.serial.log").read_text(errors="replace")
    runtime_records = [line for line in serial.splitlines() if '"logger":"environment-runtime-lifecycle"' in line]
    assert runtime_records, "packaged Runtime did not emit lifecycle records on the guest serial console"
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((gateway_address, 9090), timeout=0.2)

    if wrong_gateway_client_ca:
        gateway_log = gateway_service.log_path.read_text(errors="replace").lower()
        assert "remote error: tls: bad certificate" in gateway_log
    assert readiness_error is None, f"Runtime/Gateway readiness failed: {readiness_error!r}"
    status = readiness.result()
    assert status.environment_runtime_boot_id


if __name__ == "__main__":
    pytest_bazel.main()
