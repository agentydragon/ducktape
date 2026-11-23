"""A real Kubernetes API server and etcd, without a scheduler or workload runtime."""

import asyncio
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.config import new_client_from_config
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.subjects import ServiceAccountRef
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


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

    async def workload_token(
        self, account: ServiceAccountRef, audiences: list[str], *, pod_name: str | None = None
    ) -> str:
        """A token for `account` bound to a Pod; the namespace, ServiceAccount and Pod are created on first use."""
        core = k8s_client.CoreV1Api(self.api)
        pod_name = account.name if pod_name is None else pod_name
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
                    metadata=k8s_client.V1ObjectMeta(name=pod_name),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=account.name,
                        containers=[k8s_client.V1Container(name="workload", image="registry.test/unused")],
                    ),
                ),
            )
        )
        return await self.pod_token(account.namespace, pod_name, audiences)

    @asynccontextmanager
    async def impersonating(self, user: str) -> AsyncIterator[k8s_client.ApiClient]:
        """A client acting as `user`, authorized as an administrator so that admission alone decides."""
        async with await new_client_from_config(config_file=str(self.kubeconfig)) as api:
            api.set_default_header("Impersonate-User", user)
            api.set_default_header("Impersonate-Group", "system:masters")
            yield api

    async def install_admission_policy(
        self, policy: dict[str, Any], binding: dict[str, Any], *, namespaces: Iterable[str]
    ) -> None:
        """Create `namespaces`, then the ValidatingAdmissionPolicy and its binding from their manifests."""
        core = k8s_client.CoreV1Api(self.api)
        for namespace in namespaces:
            await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
        admission = k8s_client.AdmissionregistrationV1Api(self.api)
        # The client serializes a manifest dict as it would its own model, which is what its stubs name.
        await admission.create_validating_admission_policy(cast(Any, policy))
        await admission.create_validating_admission_policy_binding(cast(Any, binding))

    async def install_crds(self, *definitions: dict[str, Any]) -> None:
        """Create the CRDs from their manifests, then wait until the API server serves every one."""
        extensions = k8s_client.ApiextensionsV1Api(self.api)
        for definition in definitions:
            await extensions.create_custom_resource_definition(cast(Any, definition))
        async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                for definition in definitions:
                    installed = await extensions.read_custom_resource_definition(definition["metadata"]["name"])
                    assert any(
                        c.type == "Established" and c.status == "True" for c in installed.status.conditions or []
                    )


async def dry_run_admitted(write: Awaitable[object]) -> bool:
    """Whether the API server admits the dry-run `write`: admission refuses with a 422, any other failure raises."""
    try:
        await write
    except k8s_client.ApiException as error:
        if error.status != 422:
            raise
        return False
    return True


async def wait_until_refused(admitted: Callable[[], Awaitable[bool]]) -> None:
    """Wait until `admitted` is refused: a new policy loads in the background, with no sign that it has."""
    async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            assert not await admitted()


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
    runfiles_root = Runfiles.CreateOrRaise().root()
    binary = runfiles_root / "ducktape/agentplane/testing/envtest/server_/server"
    api_server = Path(str(runfiles_root / "envtest_kubernetes_linux_amd64/kube-apiserver"))
    assets = api_server.parent
    crds = runfiles_root / "ducktape/agentplane/crds/manifests"
    log_path = undeclared_outputs_dir() / f"kubernetes-apiserver-{uuid4().hex}.log"
    with tempfile.TemporaryDirectory(prefix="agentplane-apiserver-") as work, log_path.open("wb") as log:
        kubeconfig = Path(work) / "kubeconfig"
        process = subprocess.Popen(
            [binary, f"--assets={assets}", f"--crds={crds}", f"--kubeconfig={kubeconfig}"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for attempt in Retrying(
                stop=stop_after_delay(60),
                wait=wait_fixed(0.05),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    if process.poll() is not None:
                        raise RuntimeError(f"Kubernetes API server exited with {process.returncode}; see {log_path}")
                    assert kubeconfig.exists(), f"Kubernetes API server is not ready; see {log_path}"
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
