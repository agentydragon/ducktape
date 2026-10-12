"""This service's own Kubernetes access, from which each sandbox or box group's inventory is built."""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes_asyncio.client import ApiClient, CoreV1Api

from agentplane.action_service.sandbox.binding import PodExecutorBinding, SandboxExecutorBinding
from agentplane.action_service.sandbox.inventory import SandboxInventory
from agentplane.action_service.sandbox.pod_inventory import PodInventory
from mcp_infra.exec.kubernetes import ExecRunner
from util.kubernetes import CustomObjectsClient


@dataclass(frozen=True)
class SandboxClients:
    """This service's own Kubernetes access, from which one inventory per configured group is built.

    Held apart from the binding because the clients are the process's and the binding is reviewed
    configuration: several groups may be configured, and they share one API client between them.
    """

    api: ApiClient
    custom_objects: CustomObjectsClient
    core_v1: CoreV1Api
    exec_runner: ExecRunner

    def inventory(self, binding: SandboxExecutorBinding) -> SandboxInventory:
        return SandboxInventory(
            binding, custom_objects=self.custom_objects, core_v1=self.core_v1, exec_runner=self.exec_runner
        )

    def pod_inventory(self, binding: PodExecutorBinding) -> PodInventory:
        return PodInventory(binding, api=self.api, core_v1=self.core_v1, exec_runner=self.exec_runner)
