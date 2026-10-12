"""Read Agentplane's Sandboxes of both kinds and the Pod under each, with no authority to change them."""

from __future__ import annotations

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api

from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxDestination
from agentplane.sandbox_resource import read_sandbox
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.sandbox_service.kubernetes_views import MANAGED_LABEL, SandboxResource, own_sandbox_view, sandbox_view
from util.agent_sandbox import SANDBOX_API, SANDBOXES_PLURAL
from util.kubernetes import CustomObjectsClient


class SandboxReader:
    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient, core_v1: CoreV1Api):
        self._namespace = namespace
        self._custom_objects = custom_objects
        self._core_v1 = core_v1

    @property
    def namespace(self) -> str:
        return self._namespace

    async def get(self, name: str) -> Sandbox:
        """The named Sandbox of either kind; a Pod's name holds one of the two."""
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self._core_v1.api_client  # type: ignore[attr-defined]
        own = await read_sandbox(self._custom_objects, self._namespace, name)
        if own is not None:
            return own_sandbox_view(own, await self._pod(name), api_client=api_client)
        return sandbox_view(await self._sandbox(name), await self._pod(name), api_client=api_client)

    async def incarnation(self, destination: SandboxDestination) -> Sandbox:
        """The named Sandbox, only if it is still that incarnation and runs as that owner."""
        if not all((destination.sandbox, destination.sandbox_uid, destination.owner.namespace, destination.owner.name)):
            raise ValueError("Sandbox name, UID, and owner are required")
        view = await self.get(destination.sandbox)
        if view.uid != destination.sandbox_uid or view.service_account != destination.owner:
            raise SandboxNotFoundError(destination.sandbox)
        return view

    async def _sandbox(self, name: str) -> SandboxResource:
        """The named Sandbox, only if it is Agentplane's: an unmanaged one is not in this inventory."""
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxNotFoundError(name) from error
            raise
        sandbox = SandboxResource.model_validate(raw)
        if sandbox.metadata.labels.get(MANAGED_LABEL) != "true":
            raise SandboxNotFoundError(name)
        return sandbox

    async def _pod(self, name: str) -> k8s_client.V1Pod | None:
        try:
            return await self._core_v1.read_namespaced_pod(name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return None
            raise


# gazelle:include_dep @pypi//protobuf
