"""The operator's writes of PolicyBindings, and the egress projections the UI reads beside them.

The app makes these Kubernetes calls for the operator who asked; the layers that execute on the
objects (Policy Controller, Sandbox Controller, Action Service, egress proxy) act on them without
editing them.
"""

from datetime import datetime

from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, PolicyBinding
from agentplane.proto.sandbox_service_pb2 import Sandbox
from agentplane.sandbox_service.egress_views import (
    EGRESS_API,
    FLUX_KUSTOMIZATION_LABEL,
    BindingNotFoundError,
    BindingView,
    EgressReader,
    FluxOwnedBindingError,
    SandboxOwnedBindingError,
    is_sandbox_controlled,
)
from agentplane.sandboxes.errors import UnknownPolicySetError
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf


class EgressAccess:
    def __init__(
        self, *, namespace: str, custom_objects: CustomObjectsClient, action_policy: ActionPolicyInventory
    ) -> None:
        self.read = EgressReader(namespace=namespace, custom_objects=custom_objects)
        self._namespace = namespace
        self._custom_objects = custom_objects
        self._action_policy = action_policy

    async def bindings_for(self, subject: ServiceAccountRef) -> list[BindingView]:
        return await self.read.bindings_for(subject)

    async def grant(self, sandbox: Sandbox, policy_sets: list[str], *, expires_at: datetime | None) -> BindingView:
        """One binding of the ServiceAccount the sandbox runs as to the PolicySets, of any rule type,
        owned by the Sandbox so its deletion garbage-collects it. Creating it is the grant: granting a
        sandbox again adds another binding rather than editing one it has, so each grant's `expiresAt`
        is its own.
        """
        await self._require_known(policy_sets)
        spec: dict[str, object] = {
            "subject": ServiceAccountRef(
                namespace=sandbox.service_account.namespace, name=sandbox.service_account.name
            ).model_dump(),
            "policySets": policy_sets,
        }
        if expires_at is not None:
            spec["expiresAt"] = expires_at.isoformat()
        created = PolicyBinding.model_validate(
            await self._custom_objects.create_namespaced_custom_object(
                *EGRESS_API,
                self._namespace,
                POLICY_BINDINGS_PLURAL,
                {
                    "apiVersion": "/".join(EGRESS_API),
                    "kind": "PolicyBinding",
                    "metadata": {
                        # The API server names it. A sandbox may be granted more than once, and a name
                        # derived from the sandbox alone would make every grant after the first a 409.
                        "generateName": f"{sandbox.name}-",
                        # Not the controller: the Sandbox controller owns the Pod, the PVC and the
                        # Sandbox's own binding, and this reference is for cascading deletion only. It
                        # cascades only while bindings and Sandboxes share a namespace — Kubernetes treats
                        # a namespaced owner in another namespace as absent and collects the dependent.
                        "ownerReferences": [
                            {
                                "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
                                "kind": sandbox_resource.SANDBOX_KIND,
                                "name": sandbox.name,
                                "uid": str(sandbox.uid),
                                "controller": False,
                                "blockOwnerDeletion": False,
                            }
                        ],
                    },
                    "spec": spec,
                },
            )
        )
        # Failure after the create must not trigger a retry, which would grant twice.
        for binding in await self.bindings_for(created.spec.subject):
            if binding.name == created.metadata.name:
                return binding
        raise ConnectionError("Created PolicyBinding is no longer visible; reconcile before retrying")

    async def revoke(self, name: str) -> None:
        """Delete a runtime binding, which is how a grant is taken back; a Flux-applied one is
        refused, git being its owner, and so is a Sandbox's own, which its controller would recreate."""
        binding = await self._binding(name)
        if FLUX_KUSTOMIZATION_LABEL in binding.metadata.labels:
            raise FluxOwnedBindingError(name)
        if is_sandbox_controlled(binding):
            raise SandboxOwnedBindingError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *EGRESS_API, self._namespace, POLICY_BINDINGS_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def _require_known(self, names: list[str]) -> None:
        if missing := sorted(set(names) - set(await self._action_policy.policy_set_names())):
            raise UnknownPolicySetError(missing, kind="PolicySet")

    async def _binding(self, name: str) -> PolicyBinding:
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *EGRESS_API, self._namespace, POLICY_BINDINGS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise BindingNotFoundError(name) from error
            raise
        return PolicyBinding.model_validate(raw)
