"""The namespace's egress policy as the app shows and edits it: EgressPolicies and EgressBindings.

The proxy (x/agentplane/egress) enforces these resources; the app reads the same objects through the
API server, presents the bindings that name a sandbox with their provenance, approval, expiry, and
resolved policies, and changes approval or deletes a binding under its own RBAC. Nothing here is in
the enforcement path: the proxy's `Active` condition is shown as written, never recomputed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient
from x.agentplane.app.identity import Caller
from x.agentplane.app.inventory import PROFILE_LABEL, Condition, InventoryError

GRANTED_BY_LABEL = "agentplane.allegedly.works/granted-by"
# The provenance a Flux-applied binding carries (cluster/k8s/agentplane-staging/egress); nothing at
# runtime touches such a binding, since Flux prunes only what it applied.
FLUX_PROVENANCE = "flux"
ACTIVE_CONDITION = "Active"

_EGRESS_API = ("agentplane.allegedly.works", "v1alpha1")
_POLICIES_PLURAL = "egresspolicies"
_BINDINGS_PLURAL = "egressbindings"
_SANDBOX_API_VERSION = "agents.x-k8s.io/v1beta1"
_MERGE_PATCH = "application/merge-patch+json"


class BindingNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no EgressBinding {name=}")
        self.name = name


class UnknownProfileError(InventoryError):
    """A profile no binding selects on: the sandbox would match nothing and nothing would say so."""

    def __init__(self, profile: str, known: list[str]) -> None:
        super().__init__(
            f"no EgressBinding selects {profile=}; the profiles that exist are {', '.join(known) or 'none'}"
        )
        self.profile = profile
        self.known = known


class FluxOwnedBindingError(InventoryError):
    """A Flux-applied binding is git's to remove; deleting it at runtime would only be re-applied."""

    def __init__(self, name: str) -> None:
        super().__init__(f"EgressBinding {name=} comes from git; remove it there")
        self.name = name


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


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


class _LabelSelector(_Wire):
    match_labels: dict[str, str] = Field(alias="matchLabels")


class _Subject(_Wire):
    """The CRD admits exactly one of the two; a subject is matched by whichever it carries."""

    sandbox: _SandboxRef | None = None
    sandbox_selector: _LabelSelector | None = Field(alias="sandboxSelector", default=None)

    def matches(self, name: str, labels: dict[str, str]) -> bool:
        if self.sandbox is not None:
            return self.sandbox.name == name
        if self.sandbox_selector is not None:
            return all(labels.get(key) == value for key, value in self.sandbox_selector.match_labels.items())
        return False

    def profile(self) -> str | None:
        """The profile this subject selects on, when its selector names the profile label."""
        if self.sandbox_selector is None:
            return None
        return self.sandbox_selector.match_labels.get(PROFILE_LABEL)


class _Approval(_Wire):
    state: ApprovalState
    by: str | None = None
    at: AwareDatetime | None = None


class _BindingSpec(_Wire):
    subjects: list[_Subject]
    policies: list[str]
    expires_at: AwareDatetime | None = Field(alias="expiresAt", default=None)
    approval: _Approval


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


class SubjectView(BaseModel):
    """How a binding names its subjects: one sandbox, or every sandbox carrying these labels."""

    model_config = ConfigDict(extra="forbid")

    sandbox: str | None = None
    match_labels: dict[str, str] | None = None


class BindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    granted_by: str | None = Field(
        description="The provenance label: flux, or the operator who granted it through the app."
    )
    from_git: bool = Field(description="Flux applied it; approval is editable, deletion is git's.")
    subjects: list[SubjectView]
    approval: ApprovalState
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    policies: list[PolicyView] = Field(description="The named policies that exist, in the binding's order.")
    missing_policies: list[str] = Field(description="Names in the binding that no EgressPolicy answers to.")
    active: bool | None = Field(description="The proxy's Active condition; absent until the proxy has written status.")
    active_reason: str | None = None
    active_message: str | None = None


class ProfileView(BaseModel):
    """A profile a sandbox can be launched with, and the bindings that make it mean something.

    A profile exists because some EgressBinding's selector names the profile label, so this is the
    same source the proxy matches a sandbox against rather than a second list to keep in step.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The profile label's value; a sandbox stamped with it matches these bindings.")
    bindings: list[BindingView] = Field(description="The bindings selecting this profile, in name order.")


class EgressInventory:
    """The namespace's policies and bindings, read and written through the API server."""

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient):
        self._namespace = namespace
        self._custom_objects = custom_objects

    async def list_policies(self) -> list[PolicyView]:
        return [_policy_view(policy) for policy in await self._policies()]

    async def list_profiles(self) -> list[ProfileView]:
        """Every profile some binding selects on, in name order: the set worth launching a sandbox with."""
        policies = await self._policy_views()
        profiles: dict[str, list[BindingView]] = defaultdict(list)
        for binding in sorted(await self._bindings(), key=lambda binding: binding.metadata.name):
            view = _binding_view(binding, policies)
            for name in sorted(
                {profile for subject in binding.spec.subjects if (profile := subject.profile()) is not None}
            ):
                profiles[name].append(view)
        return [ProfileView(name=name, bindings=bindings) for name, bindings in sorted(profiles.items())]

    async def require_profile(self, profile: str) -> None:
        """Refuse a profile no binding selects: such a sandbox reaches less than asked for, silently."""
        known = [view.name for view in await self.list_profiles()]
        if profile not in known:
            raise UnknownProfileError(profile, known)

    async def bindings_for(self, sandbox: str, labels: dict[str, str]) -> list[BindingView]:
        """Every binding with a subject naming the sandbox or selecting its labels, in name order."""
        policies = await self._policy_views()
        return sorted(
            (
                _binding_view(binding, policies)
                for binding in await self._bindings()
                if any(subject.matches(sandbox, labels) for subject in binding.spec.subjects)
            ),
            key=lambda view: view.name,
        )

    async def approve(self, name: str, *, by: Caller) -> None:
        await self._decide(name, ApprovalState.APPROVED, by)

    async def deny(self, name: str, *, by: Caller) -> None:
        await self._decide(name, ApprovalState.DENIED, by)

    async def revoke(self, name: str) -> None:
        """Delete a runtime binding; a Flux-applied one is refused, git being its owner."""
        binding = await self._binding(name)
        if binding.metadata.labels.get(GRANTED_BY_LABEL) == FLUX_PROVENANCE:
            raise FluxOwnedBindingError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *_EGRESS_API, self._namespace, _BINDINGS_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def grant(self, *, sandbox: str, sandbox_uid: UUID, policies: list[str], by: Caller) -> None:
        """The launch-time pick: one approved binding of the sandbox to the policies, approved by
        and labelled as granted by `by`, owned by the Sandbox so its deletion garbage-collects it."""
        body = {
            "apiVersion": "/".join(_EGRESS_API),
            "kind": "EgressBinding",
            "metadata": {
                "name": f"{sandbox}-picked",
                "labels": {GRANTED_BY_LABEL: by.label},
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
            "spec": {
                "subjects": [{"sandbox": {"name": sandbox}}],
                "policies": policies,
                "approval": {"state": ApprovalState.APPROVED, "by": by.name, "at": _now()},
            },
        }
        await self._custom_objects.create_namespaced_custom_object(
            *_EGRESS_API, self._namespace, _BINDINGS_PLURAL, body
        )

    async def _decide(self, name: str, state: ApprovalState, by: Caller) -> None:
        await self._binding(name)
        await self._custom_objects.patch_namespaced_custom_object(
            *_EGRESS_API,
            self._namespace,
            _BINDINGS_PLURAL,
            name,
            {"spec": {"approval": {"state": state, "by": by.name, "at": _now()}}},
            _content_type=_MERGE_PATCH,
        )

    async def _policies(self) -> list[_EgressPolicy]:
        page = await self._custom_objects.list_namespaced_custom_object(*_EGRESS_API, self._namespace, _POLICIES_PLURAL)
        return [_EgressPolicy.model_validate(item) for item in _ResourceList.model_validate(page).items]

    async def _policy_views(self) -> dict[str, PolicyView]:
        return {policy.metadata.name: _policy_view(policy) for policy in await self._policies()}

    async def _bindings(self) -> list[_EgressBinding]:
        page = await self._custom_objects.list_namespaced_custom_object(*_EGRESS_API, self._namespace, _BINDINGS_PLURAL)
        return [_EgressBinding.model_validate(item) for item in _ResourceList.model_validate(page).items]

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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    granted_by = binding.metadata.labels.get(GRANTED_BY_LABEL)
    active = next((c for c in binding.status.conditions if c.type == ACTIVE_CONDITION), None)
    return BindingView(
        name=binding.metadata.name,
        granted_by=granted_by,
        from_git=granted_by == FLUX_PROVENANCE,
        subjects=[
            SubjectView(
                sandbox=subject.sandbox.name if subject.sandbox is not None else None,
                match_labels=subject.sandbox_selector.match_labels if subject.sandbox_selector is not None else None,
            )
            for subject in binding.spec.subjects
        ],
        approval=binding.spec.approval.state,
        approved_by=binding.spec.approval.by,
        approved_at=binding.spec.approval.at,
        expires_at=binding.spec.expires_at,
        policies=[policies[name] for name in binding.spec.policies if name in policies],
        missing_policies=[name for name in binding.spec.policies if name not in policies],
        active=None if active is None else active.status == "True",
        active_reason=active.reason if active is not None else None,
        active_message=active.message if active is not None else None,
    )
