"""Service-owned Action-policy grant provisioning."""

from collections.abc import Sequence

from google.protobuf.json_format import MessageToDict

from agentplane.action_service.policies.resources import BINDINGS_PLURAL, ActionPolicySet, InvalidResource
from agentplane.sandbox_service.action_policy_views import (
    ACTION_POLICY_API,
    MANAGED_BY_APP,
    MANAGED_BY_LABEL,
    ActionPolicyReader,
    UnknownPolicySetError,
)
from agentplane.sandbox_service.kubevirt_contract import KUBEVIRT_API_VERSION, VM_KIND
from agentplane.sandbox_service.models import EnvironmentKind, environment_kind
from agentplane.sandbox_service.owned_binding import create_binding
from agentplane.sandbox_service.protocol_pb2 import Sandbox
from util.agent_sandbox import SANDBOX_API, SANDBOX_KIND


class ActionPolicyBindings(ActionPolicyReader):
    """Service-owned mutations of Kubernetes policy bindings."""

    async def require_policy_sets(self, names: Sequence[str]) -> None:
        """Every name must resolve to a set the namespace holds, or nothing is written."""
        _require_known(names, await self._policy_sets_by_name())

    async def bind(self, sandbox: Sandbox, policy_sets: Sequence[str], *, initial: bool = False) -> None:
        """One binding of the ServiceAccount the sandbox runs as to the sets, owned by the Sandbox
        so its deletion garbage-collects it. Creating it is the whole grant; the Action Service
        reads `spec` and learns nothing of which preset chose the sets.
        """
        _require_known(policy_sets, await self._policy_sets_by_name())
        await create_binding(
            self._custom_objects,
            *ACTION_POLICY_API,
            self._namespace,
            BINDINGS_PLURAL,
            {
                "apiVersion": "/".join(ACTION_POLICY_API),
                "kind": "ActionPolicyBinding",
                "metadata": {
                    # The API server names it, as it does the egress binding: a Sandbox may be
                    # bound again later, and a name derived from the Sandbox alone would 409.
                    **(
                        {"name": f"ap-init-{sandbox.uid.replace('-', '')}"}
                        if initial
                        else {"generateName": f"{sandbox.name}-"}
                    ),
                    "labels": {MANAGED_BY_LABEL: MANAGED_BY_APP},
                    # Not the controller: the Sandbox controller owns the Pod and PVC, and this
                    # reference is for cascading deletion only. The binding lives in the Sandbox's
                    # namespace, which is also where the Action Service matches it to the caller,
                    # so the cascade holds.
                    "ownerReferences": [
                        {
                            "apiVersion": KUBEVIRT_API_VERSION
                            if environment_kind(sandbox.kind) == EnvironmentKind.KUBEVIRT
                            else SANDBOX_API.api_version,
                            "kind": VM_KIND
                            if environment_kind(sandbox.kind) == EnvironmentKind.KUBEVIRT
                            else SANDBOX_KIND,
                            "name": sandbox.name,
                            "uid": str(sandbox.uid),
                            "controller": False,
                            "blockOwnerDeletion": False,
                        }
                    ],
                },
                "spec": {
                    "subject": MessageToDict(sandbox.service_account, preserving_proto_field_name=True),
                    "policySets": list(policy_sets),
                },
            },
        )


def _require_known(names: Sequence[str], policy_sets: dict[str, ActionPolicySet | InvalidResource]) -> None:
    if unknown := [name for name in names if name not in policy_sets]:
        raise UnknownPolicySetError(unknown)
