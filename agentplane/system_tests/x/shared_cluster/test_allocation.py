"""Allocation, token boundaries and cleanup against a real Kubernetes API server."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.system_tests.x.shared_cluster import allocation, permissions
from agentplane.system_tests.x.shared_cluster.namespaces import TENANT_LABEL, TENANT_VALUE, Namespaces
from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

_QUOTA = {"pods": "8", "persistentvolumeclaims": "2", "requests.storage": "4Gi"}


async def _finish_deletion(core: k8s.CoreV1Api, name: str) -> None:
    # envtest deliberately has no namespace controller. Complete its namespace's
    # finalization through the actual API after testing that cleanup retains capacity.
    namespace = await core.read_namespace(name)
    namespace.spec.finalizers = []
    await core.replace_namespace_finalize(name, namespace)
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.1), reraise=True):
        with attempt, pytest.raises(ApiException, match="Not Found"):
            await core.read_namespace(name)


async def test_allocation_mints_scoped_credentials_and_holds_capacity_until_cleanup(
    tmp_path: Path, runfiles: Runfiles
) -> None:
    async with kubernetes_apiserver() as server:
        output = tmp_path / "credentials"
        api_server = server.api.configuration.host
        assert api_server is not None
        process = await asyncio.create_subprocess_exec(
            runfiles.root() / os.environ["LEASE_BINARY_RLOCATION"],
            "--admin-kubeconfig",
            str(server.kubeconfig),
            "allocate",
            "--server",
            api_server,
            "--output",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert output.stat().st_mode & 0o777 == 0o700
        for path in output.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600
        kubeconfig = json.loads((output / "observer.kubeconfig").read_text())
        assert kubeconfig["users"] == [{"name": "observer", "user": {"token": kubeconfig["users"][0]["user"]["token"]}}]
        credentials = allocation.Credentials(
            allocation=allocation.Allocation.model_validate_json((output / "allocation.json").read_text()),
            application_token=(output / "application-token").read_text(),
            observer_token=kubeconfig["users"][0]["user"]["token"],
        )
        owned = credentials.allocation
        names = Namespaces(owned.run_id)
        assert stdout.decode().strip() == names.application
        assert credentials.application_token not in stdout.decode()
        assert credentials.observer_token not in stdout.decode()
        core = k8s.CoreV1Api(server.api)
        for name in names.all:
            namespace = await core.read_namespace(name)
            assert namespace.metadata.labels[TENANT_LABEL] == TENANT_VALUE
            assert namespace.metadata.labels["pod-security.kubernetes.io/enforce"] == "baseline"
            assert namespace.metadata.labels[allocation.TTL_LABEL] == owned.expires_at.strftime("%Y-%m-%dT%H%M%SZ")
            quota = await core.read_namespaced_resource_quota("test-run", name)
            assert quota.spec.hard
        assert credentials.application_token not in repr(credentials)
        assert credentials.observer_token not in repr(credentials)
        authenticated = await k8s.AuthenticationV1Api(server.api).create_token_review(
            k8s.V1TokenReview(spec=k8s.V1TokenReviewSpec(token=credentials.application_token, audiences=["agentplane"]))
        )
        assert authenticated.status.authenticated
        assert (
            authenticated.status.user.username == f"system:serviceaccount:{names.application}:{allocation.APPLICATION}"
        )

        configuration = k8s.Configuration()
        configuration.host = server.api.configuration.host
        configuration.ssl_ca_cert = server.api.configuration.ssl_ca_cert
        configuration.api_key = {"BearerToken": credentials.observer_token}
        configuration.api_key_prefix = {"BearerToken": "Bearer"}
        async with k8s.ApiClient(configuration) as observer:
            async for attempt in AsyncRetrying(stop=stop_after_delay(15), wait=wait_fixed(0.1), reraise=True):
                with attempt:
                    await permissions.check_permissions(api_client=observer, namespace=names.application)
            for other in (names.credentials, names.monitoring, Namespaces(uuid4()).application):
                review = await k8s.AuthorizationV1Api(observer).create_self_subject_access_review(
                    k8s.V1SelfSubjectAccessReview(
                        spec=k8s.V1SelfSubjectAccessReviewSpec(
                            resource_attributes=k8s.V1ResourceAttributes(
                                group="", resource="pods", subresource="log", verb="get", namespace=other
                            )
                        )
                    )
                )
                assert not review.status.allowed
        with pytest.raises(allocation.CapacityUnavailableError):
            await allocation.allocate(api=server.api, lifetime=timedelta(hours=1), quota=_QUOTA)
        assert await allocation.cleanup(api=server.api, allocation=owned) is False
        for name in names.all:
            assert (await core.read_namespace(name)).metadata.deletion_timestamp is not None
        with pytest.raises(allocation.CapacityUnavailableError):
            await allocation.allocate(api=server.api, lifetime=timedelta(hours=1), quota=_QUOTA)
        for name in names.all:
            await _finish_deletion(core, name)
        assert await allocation.cleanup(api=server.api, allocation=owned) is True
        assert await allocation.cleanup(api=server.api, allocation=owned) is True


async def test_expired_capacity_is_not_reassigned_while_namespaces_remain() -> None:
    async with kubernetes_apiserver() as server:
        credentials = await allocation.allocate(api=server.api, lifetime=timedelta(hours=1), quota=_QUOTA)
        coordination = k8s.CoordinationV1Api(server.api)
        lease = await coordination.read_namespaced_lease(allocation.CAPACITY_LEASE, allocation.CAPACITY_NAMESPACE)
        lease.spec.acquire_time = datetime.now(UTC) - timedelta(hours=2)
        await coordination.replace_namespaced_lease(allocation.CAPACITY_LEASE, allocation.CAPACITY_NAMESPACE, lease)
        with pytest.raises(allocation.CapacityUnavailableError):
            await allocation.allocate(api=server.api, lifetime=timedelta(hours=1), quota=_QUOTA)
        await allocation.cleanup(api=server.api, allocation=credentials.allocation)
        core = k8s.CoreV1Api(server.api)
        for name in credentials.allocation.namespace_uids:
            await _finish_deletion(core, name)
        # No cleanup callback survived: the next allocation reclaims the expired lease,
        # but only after observing that every old namespace is actually absent.
        replacement = await allocation.allocate(api=server.api, lifetime=timedelta(hours=1), quota=_QUOTA)
        assert replacement.allocation.run_id != credentials.allocation.run_id


async def test_cleanup_cannot_delete_a_replacement_namespace() -> None:
    async with kubernetes_apiserver() as server:
        credentials = await allocation.allocate(api=server.api, lifetime=timedelta(hours=1), quota=_QUOTA)
        owned = credentials.allocation
        first = Namespaces(owned.run_id).application
        wrong_uid = owned.model_copy(update={"namespace_uids": owned.namespace_uids | {first: uuid4()}})
        with pytest.raises(ApiException) as error:
            await allocation.cleanup(api=server.api, allocation=wrong_uid)
        assert error.value.status == 409
        assert (await k8s.CoreV1Api(server.api).read_namespace(first)).metadata.deletion_timestamp is None


if __name__ == "__main__":
    pytest_bazel.main()
