"""Kubernetes container executor — runs agent containers as k8s pods.

Implements ContainerExecutor using the kubernetes_asyncio client.
Agent pods are bare pods with restartPolicy=Never (not Jobs),
because the GraderSupervisor already handles reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from kubernetes_asyncio import client
from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    CoreV1Api,
    V1Container,
    V1EnvVar,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
)

from props.orchestration.executor import ContainerResult, Exited, TimedOut

logger = logging.getLogger(__name__)


@dataclass
class K8sPodHandle:
    """Handle to a running Kubernetes pod."""

    name: str
    namespace: str
    core_v1: CoreV1Api = field(repr=False)

    async def wait(self, *, timeout_seconds: int | None) -> ContainerResult:
        """Wait for pod to reach a terminal phase, then capture logs."""
        v1 = self.core_v1

        async def _wait_for_terminal() -> str:
            """Poll pod status until Succeeded or Failed. Returns phase."""
            while True:
                pod = await v1.read_namespaced_pod(name=self.name, namespace=self.namespace)
                phase = pod.status.phase
                if phase in ("Succeeded", "Failed"):
                    return str(phase)
                await asyncio.sleep(2)

        try:
            if timeout_seconds is not None:
                phase = await asyncio.wait_for(_wait_for_terminal(), timeout=timeout_seconds)
            else:
                phase = await _wait_for_terminal()
        except TimeoutError:
            logger.error("Pod %s timed out after %d seconds", self.name, timeout_seconds)
            try:
                await v1.delete_namespaced_pod(name=self.name, namespace=self.namespace, grace_period_seconds=0)
            except ApiException as e:
                logger.warning("Failed to delete timed-out pod %s: %s", self.name, e)
            return ContainerResult(stdout="", stderr="", exit=TimedOut())

        # Capture logs (k8s merges stdout/stderr into a single stream)
        stdout = ""
        try:
            stdout = await v1.read_namespaced_pod_log(name=self.name, namespace=self.namespace)
        except ApiException as e:
            logger.warning("Failed to read logs for pod %s: %s", self.name, e)

        exit_code = _extract_exit_code(await v1.read_namespaced_pod(name=self.name, namespace=self.namespace))
        logger.info("Pod %s finished with phase=%s exit_code=%d", self.name, phase, exit_code)
        return ContainerResult(stdout=stdout, stderr="", exit=Exited(exit_code=exit_code))

    async def kill_and_delete(self) -> None:
        """Delete a pod. Best-effort."""
        try:
            await self.core_v1.delete_namespaced_pod(name=self.name, namespace=self.namespace, grace_period_seconds=0)
            logger.info("Deleted pod %s", self.name)
        except ApiException as e:
            logger.warning("Failed to delete pod %s: %s", self.name, e)


def _extract_exit_code(pod: V1Pod) -> int:
    """Extract exit code from pod's container statuses."""
    if pod.status and pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.state and cs.state.terminated:
                return int(cs.state.terminated.exit_code)
    # If pod succeeded but we can't find exit code, assume 0
    if pod.status and pod.status.phase == "Succeeded":
        return 0
    return 1


class K8sExecutor:
    """Runs agent containers as Kubernetes pods.

    Pods use restartPolicy=Never and are deleted after completion.
    The GraderSupervisor manages long-lived grader pod lifecycle.
    """

    def __init__(
        self, *, api_client: ApiClient, core_v1: CoreV1Api, namespace: str, image_pull_secret: str | None = None
    ) -> None:
        self._api_client = api_client
        self._core_v1 = core_v1
        self._namespace = namespace
        self._image_pull_secret = image_pull_secret

    async def ensure_image(self, image_ref: str) -> str:
        """No-op for k8s — kubelet pulls images on pod creation."""
        logger.debug("K8s executor: image %s will be pulled by kubelet", image_ref)
        return image_ref

    async def run_container(
        self, *, name: str, image_id: str, env: dict[str, str], labels: dict[str, str]
    ) -> K8sPodHandle:
        """Create and start a pod in the configured namespace."""
        v1 = self._core_v1

        container = V1Container(name="agent", image=image_id, env=[V1EnvVar(name=k, value=v) for k, v in env.items()])

        pod_spec = V1PodSpec(containers=[container], restart_policy="Never", automount_service_account_token=False)

        if self._image_pull_secret:
            pod_spec.image_pull_secrets = [client.V1LocalObjectReference(name=self._image_pull_secret)]

        pod = V1Pod(metadata=V1ObjectMeta(name=name, namespace=self._namespace, labels=labels), spec=pod_spec)

        await v1.create_namespaced_pod(namespace=self._namespace, body=pod)
        logger.info("Created pod %s in namespace %s", name, self._namespace)
        return K8sPodHandle(name=name, namespace=self._namespace, core_v1=self._core_v1)

    async def close(self) -> None:
        """Close the k8s API client."""
        await self._api_client.close()
