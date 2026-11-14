"""Exercise the Python owner of the real Notification subprocess without a guest VM."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel

from agentplane.crd_group import GROUP, VERSION
from agentplane.notification_service.api.notifications import notifications_pb2, notifications_pb2_grpc
from agentplane.notification_service.testing.process_fixture.process import notification_process
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, kubernetes_apiserver
from agentplane.testing.tls import issue_workload_leaf, make_ca, workload_uri
from util.testing.postgres import create_temporary_database

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep @pypi//protobuf

pytest_plugins = ("util.testing.postgres_fixtures",)


@pytest.fixture
async def api_server() -> AsyncIterator[KubernetesApiServer]:
    async with kubernetes_apiserver() as server:
        yield server


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster(api_server) as started:
        yield started


async def test_python_process_reads_owned_seed_rotates_and_stops(
    postgres_admin_url: str, api_server: KubernetesApiServer, sandbox: SandboxCluster, tmp_path: Path
) -> None:
    ca = make_ca("notification-python-process-test")
    certificate, key = issue_workload_leaf(ca, sandbox.environment_runtime_identity_uri, tmp_path)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca.cert_pem, private_key=key.read_bytes(), certificate_chain=certificate.read_bytes()
    )
    with create_temporary_database(postgres_admin_url, f"notification_python_{uuid4().hex}") as database_url:
        async with (
            notification_process(
                database_url=database_url, api=api_server, sandbox=sandbox, ca=ca, directory=tmp_path / "notification"
            ) as notification,
            grpc.aio.secure_channel(notification.address, credentials) as channel,
        ):
            stub = notifications_pb2_grpc.NotificationsStub(channel)
            listed = await stub.ListSubscriptions(notifications_pb2.ListSubscriptionsRequest(), timeout=5)
            assert len(listed.subscriptions) == 1
            row = listed.subscriptions[0]
            assert row.id == str(notification.subscription_id)
            assert row.idempotency_key == notification.idempotency_key
            assert row.source.actions.request_id == str(notification.request_id)
            before = await sandbox.custom_objects.get_namespaced_custom_object(
                GROUP, VERSION, sandbox.namespace, "workloadcredentials", sandbox.name
            )
            credential_id = uuid4().hex
            await sandbox.custom_objects.patch_namespaced_custom_object_status(
                GROUP,
                VERSION,
                sandbox.namespace,
                "workloadcredentials",
                sandbox.name,
                {
                    "status": {
                        "credentialID": credential_id,
                        "uri": workload_uri(sandbox.namespace, sandbox.name, credential_id),
                        "secretName": f"svid-{sandbox.name}-{credential_id}",
                    }
                },
                _content_type="application/merge-patch+json",
            )
            after = await sandbox.custom_objects.get_namespaced_custom_object(
                GROUP, VERSION, sandbox.namespace, "workloadcredentials", sandbox.name
            )
            assert after["metadata"]["uid"] == before["metadata"]["uid"]
            with pytest.raises(grpc.aio.AioRpcError) as revoked:
                await stub.ListSubscriptions(notifications_pb2.ListSubscriptionsRequest(), timeout=5)
            assert revoked.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert notification.process.returncode == 0


if __name__ == "__main__":
    pytest_bazel.main()
