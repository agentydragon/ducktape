"""A direct mTLS client for the Runner's public control listener in subprocess tests."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import grpc

from agentplane.proto import runner_state_pb2
from agentplane.runner import runner_service_pb2, runner_service_pb2_grpc
from agentplane.runner.configuration import RunnerControl, RunnerTransportTLS
from agentplane.testing.tls import (
    RUNNER_GATEWAY_TEST_DNS,
    CertificateAuthority,
    issue_workload_leaf,
    make_ca,
    runner_identity_uri,
    write_ca,
)

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class _RunnerMetadataInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    def __init__(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self._metadata = metadata

    async def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.aio.ClientCallDetails, Any], Awaitable[Any]],
        call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        details = grpc.aio.ClientCallDetails(
            call_details.method,
            call_details.timeout,
            grpc.aio.Metadata(*(tuple(call_details.metadata or ()) + self._metadata)),
            call_details.credentials,
            call_details.wait_for_ready,
        )
        return await continuation(details, request)


class ControlListener:
    """Observe Runner facts and call its RPCs as the mTLS-authenticated Gateway."""

    def __init__(self, gateway_cert: Path, gateway_key: Path, ca_file: Path) -> None:
        self.connections = 0
        self.facts: list[runner_state_pb2.RunnerFacts] = []
        self.facts_published = asyncio.Condition()
        self.incarnation_uid = "test-incarnation"
        self._gateway_cert = gateway_cert
        self._gateway_key = gateway_key
        self._ca_file = ca_file
        self._runner: runner_service_pb2_grpc.RunnerAsyncStub | None = None
        self._channel: grpc.aio.Channel | None = None
        self._watch_channel: grpc.aio.Channel | None = None
        self._watch: grpc.aio.UnaryStreamCall | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Condition()
        self._ready_connection = 0
        self._connection_epoch = 0

    async def connect(self, endpoint: RunnerControl, sandbox_uid: str) -> None:
        """Dial the Runner and establish its state watch, retrying until it is ready."""
        await self.disconnect()
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            self._connection_epoch += 1
            channel = await self._new_channel(endpoint)
            watch = runner_service_pb2_grpc.RunnerStub(channel).WatchState(
                runner_service_pb2.WatchStateRequest(
                    sandbox_uid=sandbox_uid,
                    incarnation_uid=self.incarnation_uid,
                    connection_epoch=self._connection_epoch,
                )
            )
            try:
                first = await asyncio.wait_for(watch.read(), timeout=2)
                if not isinstance(first, runner_service_pb2.WatchStateResponse) or not first.runner_boot_id:
                    raise RuntimeError("Runner WatchState ended before publishing its boot ID")
            except grpc.aio.AioRpcError, OSError, TimeoutError, RuntimeError:
                watch.cancel()
                await channel.close()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"Runner at {endpoint.listen_address} did not publish WatchState")
                await asyncio.sleep(min(0.1, remaining))
                continue

            metadata = (
                ("agentplane-sandbox-uid", sandbox_uid),
                ("agentplane-incarnation-uid", self.incarnation_uid),
                ("agentplane-runner-boot-id", first.runner_boot_id),
                ("agentplane-connection-epoch", str(self._connection_epoch)),
            )
            authenticated_channel = await self._new_channel(endpoint, metadata)
            self._channel = authenticated_channel
            self._watch_channel = channel
            self._watch = watch
            self._runner = runner_service_pb2_grpc.RunnerStub(authenticated_channel)
            self.connections += 1
            await self._record_facts(first.facts)
            self._ready_connection = self.connections
            self._watch_task = asyncio.create_task(self._read_facts(watch))
            async with self._connected:
                self._connected.notify_all()
            return

    async def _new_channel(
        self, endpoint: RunnerControl, metadata: tuple[tuple[str, str], ...] = ()
    ) -> grpc.aio.Channel:
        root_certificates, private_key, certificate_chain = await asyncio.gather(
            asyncio.to_thread(Path(endpoint.tls.ca_file).read_bytes),
            asyncio.to_thread(self._gateway_key.read_bytes),
            asyncio.to_thread(self._gateway_cert.read_bytes),
        )
        credentials = grpc.ssl_channel_credentials(
            root_certificates=root_certificates, private_key=private_key, certificate_chain=certificate_chain
        )
        return grpc.aio.secure_channel(
            endpoint.listen_address,
            credentials,
            options=(("grpc.ssl_target_name_override", RUNNER_GATEWAY_TEST_DNS),),
            interceptors=(_RunnerMetadataInterceptor(metadata),) if metadata else (),
        )

    async def disconnect(self) -> None:
        task, self._watch_task = self._watch_task, None
        watch, self._watch = self._watch, None
        channel, self._channel = self._channel, None
        watch_channel, self._watch_channel = self._watch_channel, None
        self._runner = None
        if watch is not None:
            watch.cancel()
        if channel is not None and channel is not watch_channel:
            await channel.close()
        if watch_channel is not None:
            await watch_channel.close()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._connected:
            self._connected.notify_all()

    async def _read_facts(self, watch: grpc.aio.UnaryStreamCall) -> None:
        try:
            async for response in watch:
                await self._record_facts(response.facts)
        except grpc.aio.AioRpcError:
            pass
        finally:
            async with self._connected:
                if self._watch is watch:
                    self._runner = None
                    self._connected.notify_all()

    async def _record_facts(self, facts: runner_state_pb2.RunnerFacts) -> None:
        snapshot = runner_state_pb2.RunnerFacts()
        snapshot.CopyFrom(facts)
        async with self.facts_published:
            self.facts.append(snapshot)
            self.facts_published.notify_all()

    async def wait_connected(self, connections: int) -> None:
        """Wait until a Runner state watch has opened at least `connections` times."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(lambda: self.connections >= connections and self._runner is not None)

    async def wait_ready(self, connections: int) -> None:
        """Wait until the Runner has published facts after accepting its latest state watch."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(
                lambda: (
                    self.connections >= connections
                    and self._runner is not None
                    and self._ready_connection == self.connections
                )
            )

    async def wait_for_facts(self, expected: runner_state_pb2.RunnerFacts) -> None:
        """Wait until a state update carries `expected` in every field except `idle_since`."""

        def carried(facts: runner_state_pb2.RunnerFacts) -> bool:
            without_idle = runner_state_pb2.RunnerFacts()
            without_idle.CopyFrom(facts)
            without_idle.ClearField("idle_since")
            return without_idle == expected

        async with asyncio.timeout(30), self.facts_published:
            await self.facts_published.wait_for(lambda: any(carried(facts) for facts in self.facts))

    async def wait_for_idleness(self, *, idle: bool) -> None:
        """Wait until the newest state update says that the Sandbox is idle or busy."""
        async with asyncio.timeout(30), self.facts_published:
            await self.facts_published.wait_for(
                lambda: bool(self.facts) and self.facts[-1].HasField("idle_since") == idle
            )

    async def runner(self) -> runner_service_pb2_grpc.RunnerAsyncStub:
        """Return the Runner service stub after its authenticated WatchState is ready."""
        await self.wait_ready(1)
        assert self._runner is not None
        return self._runner


@dataclass
class Gateway:
    """A direct Runner client that supplies per-Sandbox server certificates to subprocesses."""

    control: ControlListener
    target: str
    ca: CertificateAuthority
    certificate_directory: Path
    ca_file: Path
    _endpoints: dict[tuple[str, str], RunnerControl] = field(default_factory=dict)

    @property
    def endpoint(self) -> RunnerControl:
        return self.endpoint_for_sandbox("test-sandboxes", "test-sandbox-uid")

    def endpoint_for_sandbox(self, namespace: str, sandbox_uid: str) -> RunnerControl:
        key = (namespace, sandbox_uid)
        if key not in self._endpoints:
            runner_uri = runner_identity_uri(namespace, sandbox_uid, "test-runner-credential")
            digest = f"{namespace}-{sandbox_uid}".replace("/", "-")
            certificate_directory = self.certificate_directory / digest
            certificate_directory.mkdir()
            cert_file, key_file = issue_workload_leaf(self.ca, runner_uri, certificate_directory)
            self._endpoints[key] = RunnerControl(
                listen_address=self.target,
                tls=RunnerTransportTLS(cert_file=str(cert_file), key_file=str(key_file), ca_file=str(self.ca_file)),
            )
        return self._endpoints[key]


@asynccontextmanager
async def serve_gateway() -> AsyncIterator[Gateway]:
    """Prepare mTLS identities for a Runner listener and its Gateway-side test client."""
    with TemporaryDirectory(prefix="runner-gateway-test-") as directory:
        certificate_directory = Path(directory)
        ca = make_ca("runner transport test CA")
        ca_file, _ = write_ca(ca, certificate_directory, "ca")
        gateway_cert, gateway_key = issue_workload_leaf(
            ca, "spiffe://agentplane/runner-gateway", certificate_directory, dns_name=RUNNER_GATEWAY_TEST_DNS
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            target = f"127.0.0.1:{listener.getsockname()[1]}"
        control = ControlListener(gateway_cert, gateway_key, ca_file)
        try:
            yield Gateway(control, target, ca, certificate_directory, ca_file)
        finally:
            await control.disconnect()
