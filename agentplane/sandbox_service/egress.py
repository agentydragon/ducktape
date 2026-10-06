"""Service-owned egress grant provisioning."""

from collections.abc import Sequence

from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from more_itertools import unique_everseen

from agentplane.egress.resources import EgressBinding
from agentplane.sandbox_service.egress_views import (
    BINDINGS_PLURAL,
    EGRESS_API,
    FLUX_KUSTOMIZATION_LABEL,
    BindingNotFoundError,
    BindingView,
    EgressReader,
    FluxOwnedBindingError,
    PolicyView,
    UnknownPolicyError,
    binding_view,
)
from agentplane.sandbox_service.owned_binding import create_binding
from agentplane.sandbox_service.protocol_pb2 import Sandbox
from util.agent_sandbox import SANDBOX_API
from util.kubernetes import CustomObjectsClient


class EgressInventory(EgressReader):
    """Service-owned mutations of Kubernetes egress-policy bindings."""

    def __init__(
        self, *, namespace: str, custom_objects: CustomObjectsClient, default_policies: Sequence[str] = ()
    ) -> None:
        super().__init__(namespace=namespace, custom_objects=custom_objects)
        self._default_policies = default_policies

    def launch_policies(self, picked: Sequence[str]) -> list[str]:
        """What a new sandbox is granted: the egress policies no sandbox works without, then what the caller
        picked. Picking a default again is not an error and does not name it twice."""
        return list(unique_everseen([*self._default_policies, *picked]))

    async def require_policies(self, names: list[str]) -> None:
        """Every name must resolve to an egress policy the namespace holds, or nothing is written."""
        _require_known(names, await self._policies_by_name())

    async def revoke(self, name: str) -> None:
        """Delete a runtime binding, which is how a grant is taken back; a Flux-applied one is
        refused, git being its owner."""
        if FLUX_KUSTOMIZATION_LABEL in (await self._binding(name)).metadata.labels:
            raise FluxOwnedBindingError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *EGRESS_API, self._namespace, BINDINGS_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def grant(self, sandbox: Sandbox, policies: list[str], *, initial: bool = False) -> BindingView:
        """One binding of the ServiceAccount the sandbox runs as to the egress policies, owned by the
        Sandbox so its deletion garbage-collects it. Creating it is the grant, at launch and
        afterwards alike: granting an already-running sandbox adds another binding rather than
        editing one it has, so each grant's `expiresAt` is its own.
        """
        known = await self._policies_by_name()
        _require_known(policies, known)
        created = await create_binding(
            self._custom_objects,
            *EGRESS_API,
            self._namespace,
            BINDINGS_PLURAL,
            {
                "apiVersion": "/".join(EGRESS_API),
                "kind": "EgressBinding",
                "metadata": {
                    # The API server names it. A sandbox may be granted more than once, and a name
                    # derived from the sandbox alone would make every grant after the first a 409.
                    **(
                        {"name": f"ap-init-{sandbox.uid.replace('-', '')}"}
                        if initial
                        else {"generateName": f"{sandbox.name}-"}
                    ),
                    # Not the controller: the Sandbox controller owns the Pod and PVC, and this
                    # reference is for cascading deletion only. It cascades only while bindings and
                    # Sandboxes share a namespace — Kubernetes treats a namespaced owner in another
                    # namespace as absent and collects the dependent — so pointing the proxy's
                    # `--rules-namespace` away from the sandboxes has to replace this with a sweep.
                    "ownerReferences": [
                        {
                            "apiVersion": SANDBOX_API.api_version,
                            "kind": "Sandbox",
                            "name": sandbox.name,
                            "uid": str(sandbox.uid),
                            "controller": False,
                            "blockOwnerDeletion": False,
                        }
                    ],
                },
                "spec": {
                    "subjects": [MessageToDict(sandbox.service_account, preserving_proto_field_name=True)],
                    "policies": policies,
                },
            },
        )
        return binding_view(EgressBinding.model_validate(created), known)

    async def _binding(self, name: str) -> EgressBinding:
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *EGRESS_API, self._namespace, BINDINGS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise BindingNotFoundError(name) from error
            raise
        return EgressBinding.model_validate(raw)


def _require_known(names: list[str], policies: dict[str, PolicyView]) -> None:
    if unknown := [name for name in names if name not in policies]:
        raise UnknownPolicyError(unknown)
