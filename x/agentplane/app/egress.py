"""The namespace's egress policy as the app shows and edits it: EgressPolicies and EgressBindings.

The proxy (x/agentplane/egress) enforces these resources; the app reads the same objects through the
API server, presents the bindings that name a sandbox with their provenance, expiry and resolved
policies, and creates or deletes a runtime binding under its own RBAC. A binding is desired state,
so creating one is the whole act of granting and deleting one is the whole act of taking it back;
there is no decision to record on it afterwards. Nothing here is in the enforcement path: the
proxy's `Active` condition is shown as written, never recomputed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient
from x.agentplane.app.inventory import Condition, InventoryError

# Flux stamps its inventory labels on everything it applies (cluster/k8s/agentplane-staging/egress);
# nothing at runtime deletes such a binding, since the next reconcile would apply it again.
FLUX_KUSTOMIZATION_LABEL = "kustomize.toolkit.fluxcd.io/name"
ACTIVE_CONDITION = "Active"

_EGRESS_API = ("agentplane.allegedly.works", "v1alpha1")
_POLICIES_PLURAL = "egresspolicies"
_BINDINGS_PLURAL = "egressbindings"
_SANDBOX_API_VERSION = "agents.x-k8s.io/v1beta1"


class BindingNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no EgressBinding {name=}")
        self.name = name


class FluxOwnedBindingError(InventoryError):
    """A Flux-applied binding is git's to remove; deleting it at runtime would only be re-applied."""

    def __init__(self, name: str) -> None:
        super().__init__(f"EgressBinding {name=} comes from git; remove it there")
        self.name = name


# Kubernetes-boundary models: the subset of each resource the app reads, parsed once off the wire.


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ObjectMeta(_Wire):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)


class _SecretKeyRef(_Wire):
    name: str
    key: str


class _Credential(_Wire):
    secret_ref: _SecretKeyRef = Field(alias="secretRef")
    header: str


class _Rule(_Wire):
    hosts: list[str]
    methods: list[str] | None = None
    paths: list[str] | None = None
    credential: _Credential | None = None


class _PolicySpec(_Wire):
    rules: list[_Rule]


class _EgressPolicy(_Wire):
    metadata: _ObjectMeta
    spec: _PolicySpec


class _SandboxRef(_Wire):
    name: str


class _Subject(_Wire):
    sandbox: _SandboxRef


class _BindingSpec(_Wire):
    subjects: list[_Subject]
    policies: list[str]
    expires_at: AwareDatetime | None = Field(alias="expiresAt", default=None)


class _BindingStatus(_Wire):
    conditions: list[Condition] = Field(default_factory=list)


class _EgressBinding(_Wire):
    metadata: _ObjectMeta
    spec: _BindingSpec
    status: _BindingStatus = Field(default_factory=_BindingStatus)


class _ResourceList(_Wire):
    items: list[dict[str, object]]


# API views.


class CredentialView(BaseModel):
    """The credential a rule substitutes, by name: the value lives only in the proxy."""

    model_config = ConfigDict(extra="forbid")

    secret: str
    key: str
    header: str = Field(description="The request header the proxy rewrites.")


class RuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hosts: list[str]
    methods: list[str] | None = Field(default=None, description="Absent admits any method.")
    paths: list[str] | None = Field(default=None, description="Absent admits any path.")
    credential: CredentialView | None = None


class PolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rules: list[RuleView]


class BindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    from_git: bool = Field(description="Flux applied it; removing it is git's.")
    subjects: list[str] = Field(description="The Sandboxes this binding names.")
    expires_at: datetime | None = None
    policies: list[PolicyView] = Field(description="The named policies that exist, in the binding's order.")
    missing_policies: list[str] = Field(description="Names in the binding that no EgressPolicy answers to.")
    active: bool | None = Field(description="The proxy's Active condition; absent until the proxy has written status.")
    active_reason: str | None = None
    active_message: str | None = None


class EgressInventory:
    """The namespace's policies and bindings, read and written through the API server."""

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient):
        self._namespace = namespace
        self._custom_objects = custom_objects

    async def list_policies(self) -> list[PolicyView]:
        return [_policy_view(policy) for policy in await self._policies()]

    async def bindings_for(self, sandbox: str) -> list[BindingView]:
        """Every binding with a subject naming the sandbox, in name order."""
        policies = {policy.metadata.name: _policy_view(policy) for policy in await self._policies()}
        page = await self._custom_objects.list_namespaced_custom_object(*_EGRESS_API, self._namespace, _BINDINGS_PLURAL)
        bindings = [_EgressBinding.model_validate(item) for item in _ResourceList.model_validate(page).items]
        return sorted(
            (
                _binding_view(binding, policies)
                for binding in bindings
                if any(subject.sandbox.name == sandbox for subject in binding.spec.subjects)
            ),
            key=lambda view: view.name,
        )

    async def revoke(self, name: str) -> None:
        """Delete a runtime binding, which is how a grant is taken back; a Flux-applied one is
        refused, git being its owner."""
        if FLUX_KUSTOMIZATION_LABEL in (await self._binding(name)).metadata.labels:
            raise FluxOwnedBindingError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *_EGRESS_API, self._namespace, _BINDINGS_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def grant(self, *, sandbox: str, sandbox_uid: UUID, policies: list[str]) -> None:
        """The launch-time pick: one binding of the sandbox to the policies, owned by the Sandbox so
        its deletion garbage-collects it. Creating it is the grant."""
        body = {
            "apiVersion": "/".join(_EGRESS_API),
            "kind": "EgressBinding",
            "metadata": {
                "name": f"{sandbox}-picked",
                # Not the controller: the Sandbox controller owns the Pod and PVC, and this reference is
                # for cascading deletion only.
                "ownerReferences": [
                    {
                        "apiVersion": _SANDBOX_API_VERSION,
                        "kind": "Sandbox",
                        "name": sandbox,
                        "uid": str(sandbox_uid),
                        "controller": False,
                        "blockOwnerDeletion": False,
                    }
                ],
            },
            "spec": {"subjects": [{"sandbox": {"name": sandbox}}], "policies": policies},
        }
        await self._custom_objects.create_namespaced_custom_object(
            *_EGRESS_API, self._namespace, _BINDINGS_PLURAL, body
        )

    async def _policies(self) -> list[_EgressPolicy]:
        page = await self._custom_objects.list_namespaced_custom_object(*_EGRESS_API, self._namespace, _POLICIES_PLURAL)
        return [_EgressPolicy.model_validate(item) for item in _ResourceList.model_validate(page).items]

    async def _binding(self, name: str) -> _EgressBinding:
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *_EGRESS_API, self._namespace, _BINDINGS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise BindingNotFoundError(name) from error
            raise
        return _EgressBinding.model_validate(raw)


def _policy_view(policy: _EgressPolicy) -> PolicyView:
    return PolicyView(
        name=policy.metadata.name,
        rules=[
            RuleView(
                hosts=rule.hosts,
                methods=rule.methods,
                paths=rule.paths,
                credential=CredentialView(
                    secret=rule.credential.secret_ref.name,
                    key=rule.credential.secret_ref.key,
                    header=rule.credential.header,
                )
                if rule.credential is not None
                else None,
            )
            for rule in policy.spec.rules
        ],
    )


def _binding_view(binding: _EgressBinding, policies: dict[str, PolicyView]) -> BindingView:
    active = next((c for c in binding.status.conditions if c.type == ACTIVE_CONDITION), None)
    return BindingView(
        name=binding.metadata.name,
        from_git=FLUX_KUSTOMIZATION_LABEL in binding.metadata.labels,
        subjects=[subject.sandbox.name for subject in binding.spec.subjects],
        expires_at=binding.spec.expires_at,
        policies=[policies[name] for name in binding.spec.policies if name in policies],
        missing_policies=[name for name in binding.spec.policies if name not in policies],
        active=None if active is None else active.status == "True",
        active_reason=active.reason if active is not None else None,
        active_message=active.message if active is not None else None,
    )
