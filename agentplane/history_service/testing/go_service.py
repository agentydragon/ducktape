"""Run the built Go History Service for consumer tests.

The service initializes its database before serving. A consumer reads it through the Python
`HistoryServiceClient` and writes it as a runner does, by pushing a journal to the ingress. A real
API server resolves the writers' WorkloadCredential records, while fixture readers and writers use
workload SVIDs.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import ExitStack, aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import yaml
from kubernetes_asyncio import client as k8s_client
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, retry_if_result, wait_fixed

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.api.ingress import session_ingress_pb2, session_ingress_pb2_grpc
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.settings import IngressSettings, Settings
from agentplane.identity.auth import listener as workload_listener
from agentplane.net.peertls.config import APP_URI, CENTRAL_SAMPLER_URI, HISTORY_SERVICE_URI, ClientTLS
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.go_process import GoProcess, go_process
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, make_ca, workload_uri, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.postgres import PostgresDatabaseTemplate
from util.db_url import render_postgresql_url
from util.net import listen_tcp

# gazelle:include_dep @pypi//protobuf

_SERVICE = Runfiles.CreateOrRaise().root() / "ducktape/agentplane/history_service/cmd/history-service_/history-service"
_DATABASE_URL_ENV = "AGENTPLANE_HISTORY_SERVICE_DATABASE_URL"
_CONFIG_FILE_ENV = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"
_GRACE_SECONDS = 10
# Bounds only a push or fold that stopped making progress.
_PROGRESS_BOUND_SECONDS = 30
# The ingress stores an announced snapshot only once history covers it, so a push meant to leave the feed state alone
# announces one beyond any history a test writes.
_UNCOVERED_CURSOR = 2**62

WRITER = ServiceAccountRef(namespace="test-sandbox-namespace", name="test-sandbox-runner")


def _go_database_url(database_url: str) -> str:
    """`database_url` as the Go services' PostgreSQL driver reads it."""
    return render_postgresql_url(database_url, "postgresql", query={"sslmode": "disable"})


def migrate_history_database(database_url: str) -> None:
    """Bring a database to the History Service's schema with the serving binary's migration-only mode."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        ca = make_ca("history migration test")
        ca_file, _ = write_ca(ca, directory, "ca")
        cert_file, key_file = issue_workload_leaf(
            ca, "spiffe://agentplane/history-ingress", directory, dns_name="localhost"
        )
        settings = Settings(
            policy_namespace="test-policy-namespace",
            rpc=workload_listener.Listener(
                listen_address="127.0.0.1:8080",
                tls=workload_listener.TLS(cert_file=cert_file, key_file=key_file),
                auth=[workload_listener.SPIFFEAuth(ca_file=ca_file)],
            ),
            ingress=IngressSettings(writer_namespaces=frozenset((WRITER.namespace,))),
        )
        config = directory / "history-service.yaml"
        config.write_text(yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True), sort_keys=False))
        result = subprocess.run(
            [_SERVICE, "--migrate-only"],
            env={_DATABASE_URL_ENV: _go_database_url(database_url), _CONFIG_FILE_ENV: str(config)},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def migrated_history_template(admin_url: str, template_name: str) -> Iterator[PostgresDatabaseTemplate]:
    """A History database template the serving binary initialized once, for tests that clone many History databases."""
    with PostgresDatabaseTemplate(admin_url, template_name) as template:
        migrate_history_database(template.url)
        yield template


@dataclass(frozen=True)
class GoHistoryService:
    database_url: str
    read_target: str
    ingress_target: str
    # Entered for the life of the service; `reader.new_client()` is an independent one.
    reader: HistoryServiceClient
    api: KubernetesApiServer
    process: GoProcess
    history_tls: ClientTLS
    reader_tls: ClientTLS
    svid_ca: CertificateAuthority
    directory: Path
    policy_namespace: str
    _writer_credentials: dict[tuple[ServiceAccountRef, str | None, str | None], grpc.ChannelCredentials] = field(
        default_factory=dict, init=False
    )

    async def writer_channel_credentials(
        self,
        writer: ServiceAccountRef,
        *,
        pod_name: str | None = None,
        identity_name: str | None = None,
        identity_uid: str | None = None,
    ) -> grpc.ChannelCredentials:
        """A live Identity workload SVID the ingress resolves through the test API server."""
        key = (writer, identity_name, identity_uid)
        if key in self._writer_credentials:
            return self._writer_credentials[key]

        writer_id = uuid4().hex
        credential_id = uuid4().hex
        workload_name = f"history-{writer_id}"
        identity_name = identity_name or f"history-{writer_id}"
        pod_name = pod_name or f"history-{writer_id[:12]}"
        pod_uid = str(uuid4())
        custom = k8s_client.CustomObjectsApi(self.api.api)
        core = k8s_client.CoreV1Api(self.api.api)
        for namespace in {writer.namespace, self.policy_namespace}:
            try:
                await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
            except k8s_client.ApiException as error:
                if error.status != 409:
                    raise

        try:
            sandbox = await custom.get_namespaced_custom_object(
                "agentplane.allegedly.works", "v1alpha1", writer.namespace, "sandboxes", writer.name
            )
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            sandbox_uid = str(uuid4())
        else:
            sandbox_uid = sandbox["metadata"]["uid"]

        api_version = "agentplane.allegedly.works/v1alpha1"
        try:
            identity = await custom.get_namespaced_custom_object(
                "agentplane.allegedly.works", "v1alpha1", self.policy_namespace, "identities", identity_name
            )
        except k8s_client.ApiException as error:
            if error.status != 404 or identity_uid is not None:
                raise
            identity = await custom.create_namespaced_custom_object(
                "agentplane.allegedly.works",
                "v1alpha1",
                self.policy_namespace,
                "identities",
                {
                    "apiVersion": api_version,
                    "kind": "Identity",
                    "metadata": {"name": identity_name},
                    "spec": {"description": f"History test writer {writer.namespace}/{writer.name}"},
                },
            )
        if identity_uid is not None and identity["metadata"]["uid"] != identity_uid:
            raise ValueError(f"History writer Identity {identity_name!r} is not the attached incarnation")
        uri = workload_uri(writer.namespace, workload_name, credential_id)
        binding = {"podUID": pod_uid}
        record = await custom.create_namespaced_custom_object(
            "agentplane.allegedly.works",
            "v1alpha1",
            writer.namespace,
            "workloadcredentials",
            {
                "apiVersion": api_version,
                "kind": "WorkloadCredential",
                "metadata": {
                    "name": workload_name,
                    "ownerReferences": [
                        {
                            "apiVersion": api_version,
                            "kind": "Sandbox",
                            "name": writer.name,
                            "uid": sandbox_uid,
                            "controller": True,
                        }
                    ],
                },
                "spec": {
                    "subject": {"identity": {"name": identity_name, "uid": identity["metadata"]["uid"]}},
                    "binding": binding,
                    "pod": {"name": pod_name, "uid": pod_uid},
                },
            },
        )
        record["status"] = {
            "observedGeneration": record["metadata"].get("generation", 1),
            "credentialID": credential_id,
            "uri": uri,
            "secretName": f"history-{writer_id}",
            "acceptedBinding": binding,
        }
        await custom.replace_namespaced_custom_object_status(
            "agentplane.allegedly.works", "v1alpha1", writer.namespace, "workloadcredentials", workload_name, record
        )

        leaf_directory = self.directory / "history-writers" / writer_id
        leaf_directory.mkdir(parents=True, exist_ok=True)
        cert_file, key_file = issue_workload_leaf(self.svid_ca, uri, leaf_directory, dns_name="localhost")
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self.svid_ca.cert_pem,
            private_key=key_file.read_bytes(),
            certificate_chain=cert_file.read_bytes(),
        )
        self._writer_credentials[key] = credentials
        return credentials

    async def push(
        self,
        session_id: UUID,
        entries: Sequence[event_log_pb2.EventEntry],
        *,
        writer: ServiceAccountRef = WRITER,
        attached: session_pb2.Attached | None = None,
    ) -> int:
        """Push `entries` of the Session's journal as `writer`'s runner does, returning the cursor committed through.

        `attached` is the runner's snapshot, announced first; without one the push stores no snapshot and leaves the
        Session's feed state as it was. Without `entries` the call only binds the Session to `writer` and announces
        `attached`.
        """
        announce = session_pb2.Attached(last_cursor=_UNCOVERED_CURSOR)
        if attached is not None:
            announce.CopyFrom(attached)
        announce.session_id = str(session_id)
        writer_credentials = await self.writer_channel_credentials(
            writer,
            identity_name=announce.execution_identity.name or None if announce.HasField("execution_identity") else None,
            identity_uid=announce.execution_identity.uid or None if announce.HasField("execution_identity") else None,
        )
        async with (
            asyncio.timeout(_PROGRESS_BOUND_SECONDS),
            grpc.aio.secure_channel(self.ingress_target, writer_credentials) as channel,
        ):
            call = session_ingress_pb2_grpc.SessionJournalIngressStub(channel).Push()

            async def acknowledged_cursor() -> int:
                acknowledgement = await call.read()
                assert isinstance(acknowledgement, session_ingress_pb2.Acknowledgement), (
                    "the ingress closed the call before acknowledging"
                )
                return acknowledgement.cursor

            await call.write(session_ingress_pb2.PushFrame(announce=announce))
            for entry in entries:
                await call.write(session_ingress_pb2.PushFrame(entry=entry))
            # Closing the call commits what arrived without acknowledging it, so wait for the acknowledgement first.
            committed = await acknowledged_cursor()
            while committed < max((entry.cursor for entry in entries), default=0):
                committed = await acknowledged_cursor()
            await call.done_writing()
            assert await call.code() == grpc.StatusCode.OK, await call.details()
            return committed

    async def until(self, holds: Callable[[], Awaitable[bool]]) -> None:
        """Return once `holds()` is true, asking at the watch's opening and after every change the fold notices."""
        async with asyncio.timeout(_PROGRESS_BOUND_SECONDS), aclosing(self.reader.watch_session_summaries()) as changes:
            async for _ in changes:
                if await holds():
                    return
        raise AssertionError("the summaries watch ended before the condition held")

    async def settled(
        self, session_id: UUID, condition: Callable[[history_service_pb2.SessionSummary], bool]
    ) -> history_service_pb2.SessionSummary:
        """The Session's summary once `condition` holds of it."""
        latest: history_service_pb2.SessionSummary | None = None

        async def holds() -> bool:
            nonlocal latest
            try:
                latest = await self.reader.get_session_summary(str(session_id))
            except ServiceError as error:
                if error.code != grpc.StatusCode.NOT_FOUND:
                    raise
                return False
            return condition(latest)

        try:
            await self.until(holds)
        except TimeoutError as error:
            error.add_note(f"the last summary of {session_id}:\n{latest}")
            raise
        assert latest is not None
        return latest

    async def folded(self, session_id: UUID, through: int) -> history_service_pb2.SessionSummary:
        """The Session's summary once the fold has reached `through`."""
        return await self.settled(session_id, lambda summary: summary.folded_through >= through)


async def wait_for_history_session(history: HistoryServiceClient, session_id: str) -> None:
    """Wait until History records the Session."""
    while True:
        async with aclosing(history.watch_recorded_sessions(after_position=0)) as recorded_sessions:
            async for recorded in recorded_sessions:
                if str(recorded.session_id) == session_id:
                    return


async def wait_for_history_snapshot(
    history: HistoryServiceClient, session_id: str, *, timeout_s: float
) -> session_pb2.Attached:
    """Wait for a recorded Session's raw feed to contain its announced snapshot."""

    async def announced() -> session_pb2.Attached | None:
        page = await history.read_session_events(session_id, after_cursor=0, limit=1)
        if not page.HasField("feed_state") or not page.feed_state.HasField("attached"):
            return None
        attached = session_pb2.Attached()
        attached.CopyFrom(page.feed_state.attached)
        attached.last_cursor = page.last_cursor
        return attached

    async with asyncio.timeout(timeout_s):
        snapshot: session_pb2.Attached | None = await AsyncRetrying(
            retry=retry_if_result(lambda result: result is None), wait=wait_fixed(0.05)
        )(announced)
    assert snapshot is not None
    return snapshot


@asynccontextmanager
async def go_history_service(
    database_url: str,
    *,
    api: KubernetesApiServer,
    directory: Path,
    writer_namespaces: Sequence[str] = (WRITER.namespace,),
    watch_lease_s: float = 300,
    sampler_service_actor: str | None = None,
    svid_ca: CertificateAuthority | None = None,
    policy_namespace: str = WRITER.namespace,
) -> AsyncIterator[GoHistoryService]:
    """The Go History Service on `database_url`, using service SVID reads and workload SVID writes."""
    with ExitStack() as listeners:
        reader_listener = listeners.enter_context(listen_tcp())
        svid_ca = svid_ca or make_ca("history test services")
        config = directory / "history-service.yaml"
        reader_fd = reader_listener.socket.fileno()
        tls_directory = directory / "history-tls"
        tls_directory.mkdir()
        ca_file, _ = write_ca(svid_ca, tls_directory, "ca")
        server_directory = tls_directory / "server"
        server_directory.mkdir()
        server_cert, server_key = issue_workload_leaf(
            svid_ca, HISTORY_SERVICE_URI, server_directory, dns_name="localhost"
        )
        reader_directory = tls_directory / "reader"
        reader_directory.mkdir()
        reader_cert, reader_key = issue_workload_leaf(svid_ca, APP_URI, reader_directory, dns_name="localhost")
        reader_channel_credentials = grpc.ssl_channel_credentials(
            root_certificates=svid_ca.cert_pem,
            private_key=reader_key.read_bytes(),
            certificate_chain=reader_cert.read_bytes(),
        )
        sampler_directory = tls_directory / "central-sampler"
        sampler_directory.mkdir()
        sampler_cert, sampler_key = issue_workload_leaf(
            svid_ca, CENTRAL_SAMPLER_URI, sampler_directory, dns_name="localhost"
        )
        history_tls = ClientTLS(
            ca_file=ca_file,
            server_uri=HISTORY_SERVICE_URI,
            cert_file=sampler_cert,
            key_file=sampler_key,
            client_uri=CENTRAL_SAMPLER_URI,
        )
        rpc = workload_listener.Listener(
            listen_address=f"fd:{reader_fd}",
            tls=workload_listener.TLS(cert_file=server_cert, key_file=server_key),
            auth=[workload_listener.SPIFFEAuth(ca_file=ca_file)],
        )
        settings = Settings(
            reader_service_callers=frozenset({APP_URI, CENTRAL_SAMPLER_URI}),
            policy_namespace=policy_namespace,
            request_timeout_s=10,
            rpc=rpc,
            kubeconfig=api.kubeconfig,
            ingress=IngressSettings(
                writer_namespaces=frozenset(writer_namespaces),
                sampler_service_actor=sampler_service_actor,
                # Tests compare the archive with the runner's journal entry by entry.
                settle_deltas=False,
            ),
            # The fold also runs when a commit announces itself; the interval only bounds a missed notice.
            fold_interval_s=0.05,
            watch_lease_s=watch_lease_s,
        )
        config.write_text(yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True), sort_keys=False))
        async with go_process(
            _SERVICE,
            env={_DATABASE_URL_ENV: _go_database_url(database_url), _CONFIG_FILE_ENV: str(config)},
            stderr=undeclared_outputs_dir() / f"history-service-{uuid4().hex}.log",
            health_port=reader_listener.port,
            grace_s=_GRACE_SECONDS,
            pass_fds=(reader_fd,),
            health_credentials=reader_channel_credentials,
        ) as process:
            listeners.close()
            read_target = f"localhost:{reader_listener.port}"
            service = GoHistoryService(
                database_url=database_url,
                read_target=read_target,
                ingress_target=read_target,
                reader=HistoryServiceClient(
                    read_target, request_timeout_s=20, channel_credentials=reader_channel_credentials
                ),
                api=api,
                process=process,
                history_tls=history_tls,
                reader_tls=ClientTLS(
                    ca_file=ca_file,
                    server_uri=HISTORY_SERVICE_URI,
                    cert_file=reader_cert,
                    key_file=reader_key,
                    client_uri=APP_URI,
                ),
                svid_ca=svid_ca,
                directory=directory,
                policy_namespace=policy_namespace,
            )
            async with service.reader:
                yield service
