"""A real Kubernetes API server and etcd, without a scheduler or workload runtime."""

import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.config import new_client_from_config

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


@asynccontextmanager
async def kubernetes_apiserver() -> AsyncIterator[KubernetesApiServer]:
    """Run Bazel-pinned envtest assets; only control-plane processes are started."""
    binary = get_required_path(own_repo_rlocation("agentplane/testing/envtest/server_/server"))
    assets = get_required_path("envtest_kubernetes_linux_amd64/kube-apiserver").parent
    crds = get_required_path(own_repo_rlocation("agentplane/crds/manifests"))
    log_path = undeclared_outputs_dir() / f"kubernetes-apiserver-{uuid4().hex}.log"
    with tempfile.TemporaryDirectory(prefix="agentplane-apiserver-") as work, log_path.open("wb") as log:
        kubeconfig = Path(work) / "kubeconfig"
        process = await asyncio.create_subprocess_exec(
            str(binary),
            f"--assets={assets}",
            f"--crds={crds}",
            f"--kubeconfig={kubeconfig}",
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(60):
                while not kubeconfig.exists():
                    if process.returncode is not None:
                        raise RuntimeError(f"Kubernetes API server exited with {process.returncode}; see {log_path}")
                    await asyncio.sleep(0.05)
            async with await new_client_from_config(config_file=str(kubeconfig)) as api:
                yield KubernetesApiServer(api=api, kubeconfig=kubeconfig)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    async with asyncio.timeout(30):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise RuntimeError(f"Kubernetes API server did not stop; see {log_path}") from None
            if process.returncode != 0:
                raise RuntimeError(f"Kubernetes API server exited with {process.returncode}; see {log_path}")
