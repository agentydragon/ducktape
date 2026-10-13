"""Service-owned egress grant provisioning."""

from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, PolicyBinding
from agentplane.proto.sandbox_service_pb2 import Sandbox
from agentplane.sandbox_service.egress_views import (
    EGRESS_API,
    FLUX_KUSTOMIZATION_LABEL,
    BindingNotFoundError,
    BindingView,
    EgressReader,
    FluxOwnedBindingError,
    PolicyView,
    SandboxOwnedBindingError,
    UnknownPolicyError,
    is_sandbox_controlled,
)


class EgressInventory(EgressReader):
    """Service-owned mutations of the PolicyBindings that grant egress."""

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

    async def grant(self, sandbox: Sandbox, policies: list[str]) -> BindingView:
        """One binding of the ServiceAccount the sandbox runs as to the PolicySets, owned by the
        Sandbox so its deletion garbage-collects it. Creating it is the grant: granting a sandbox again
        adds another binding rather than editing one it has, so each grant's `expiresAt` is its own.
        """
        known = await self._policies_by_name()
        _require_known(policies, known)
        created = await self._custom_objects.create_namespaced_custom_object(
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
                "spec": {
                    "subject": MessageToDict(sandbox.service_account, preserving_proto_field_name=True),
                    "policySets": policies,
                },
            },
        )
        binding = PolicyBinding.model_validate(created)
        return BindingView(
            name=binding.metadata.name,
            from_git=False,
            subjects=[binding.spec.subject],
            expires_at=binding.spec.expires_at,
            policy_sets=policies,
            policies=[known[name] for name in policies],
            missing_policies=[],
        )

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


def _require_known(names: list[str], policies: dict[str, PolicyView]) -> None:
    if unknown := [name for name in names if name not in policies]:
        raise UnknownPolicyError(unknown)
