"""A real Kubernetes API server and etcd, without a scheduler or workload runtime."""

import asyncio
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.config import new_client_from_config

from agentplane.subjects import ServiceAccountRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.undeclared_outputs import undeclared_outputs_dir


@dataclass(frozen=True)
class KubernetesApiServer:
    api: k8s_client.ApiClient
    kubeconfig: Path

    async def pod_token(self, namespace: str, pod: str, audiences: list[str]) -> str:
        """Issue a real, audience-scoped ServiceAccount token bound to the existing Pod UID."""
        core = k8s_client.CoreV1Api(self.api)
        workload = await core.read_namespaced_pod(pod, namespace)
        request = k8s_client.AuthenticationV1TokenRequest(
            spec=k8s_client.V1TokenRequestSpec(
                audiences=audiences,
                bound_object_ref=k8s_client.V1BoundObjectReference(
                    api_version="v1", kind="Pod", name=pod, uid=workload.metadata.uid
                ),
            )
        )
        response = await core.create_namespaced_service_account_token(
            workload.spec.service_account_name, namespace, request
        )
        return response.status.token

    async def workload_token(self, account: ServiceAccountRef, audiences: list[str]) -> str:
        """A token for `account` bound to a Pod of its name; the namespace, ServiceAccount and Pod are created on first use."""
        core = k8s_client.CoreV1Api(self.api)
        await _created_or_present(
            core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=account.namespace)))
        )
        await _created_or_present(
            core.create_namespaced_service_account(
                account.namespace, k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=account.name))
            )
        )
        await _created_or_present(
            core.create_namespaced_pod(
                account.namespace,
                k8s_client.V1Pod(
                    metadata=k8s_client.V1ObjectMeta(name=account.name),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=account.name,
                        containers=[k8s_client.V1Container(name="workload", image="registry.test/unused")],
                    ),
                ),
            )
        )
        return await self.pod_token(account.namespace, account.name, audiences)


async def _created_or_present(creation: Awaitable[object]) -> None:
    try:
        await creation
    except k8s_client.ApiException as error:
        if error.status != 409:
            raise


@contextmanager
def control_plane() -> Iterator[Path]:
    """Run Bazel-pinned envtest assets until exit, yielding the kubeconfig of the ready API server.

    Only control-plane processes are started. No event loop is involved, so a session-scoped fixture can share
    one control plane among tests whose loops differ; each test enters `kubernetes_api` in its own loop.
    """
    binary = get_required_path(own_repo_rlocation("agentplane/testing/envtest/server_/server"))
    assets = get_required_path("envtest_kubernetes_linux_amd64/kube-apiserver").parent
    crds = get_required_path(own_repo_rlocation("agentplane/crds/manifests"))
    log_path = undeclared_outputs_dir() / f"kubernetes-apiserver-{uuid4().hex}.log"
    with tempfile.TemporaryDirectory(prefix="agentplane-apiserver-") as work, log_path.open("wb") as log:
        kubeconfig = Path(work) / "kubeconfig"
        process = subprocess.Popen(
            [binary, f"--assets={assets}", f"--crds={crds}", f"--kubeconfig={kubeconfig}"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while not kubeconfig.exists():
                if process.poll() is not None:
                    raise RuntimeError(f"Kubernetes API server exited with {process.returncode}; see {log_path}")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Kubernetes API server was not ready within 60 s; see {log_path}")
                time.sleep(0.05)
            yield kubeconfig
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError(f"Kubernetes API server did not stop; see {log_path}") from None
            if process.returncode != 0:
                raise RuntimeError(f"Kubernetes API server exited with {process.returncode}; see {log_path}")


@asynccontextmanager
async def kubernetes_api(kubeconfig: Path) -> AsyncIterator[KubernetesApiServer]:
    """A client of the API server `kubeconfig` reaches, bound to the loop that enters it."""
    async with await new_client_from_config(config_file=str(kubeconfig)) as api:
        yield KubernetesApiServer(api=api, kubeconfig=kubeconfig)


@asynccontextmanager
async def kubernetes_apiserver() -> AsyncIterator[KubernetesApiServer]:
    """A control plane of this test's own."""
    async with AsyncExitStack() as stack:
        control = ExitStack()
        stack.push_async_callback(asyncio.to_thread, control.close)
        kubeconfig = await asyncio.to_thread(control.enter_context, control_plane())
        yield await stack.enter_async_context(kubernetes_api(kubeconfig))
