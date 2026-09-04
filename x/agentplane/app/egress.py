"""The namespace's egress policy as the app shows and edits it: EgressPolicies, EgressBindings and the EgressCredentials they name.

The proxy (x/agentplane/egress) enforces these resources; the app reads the same objects through the
API server, presents the bindings that name a sandbox with their provenance, expiry and resolved
policies, and creates or deletes a runtime binding under its own RBAC. A binding is desired state,
so creating one is the whole act of granting and deleting one is the whole act of taking it back;
there is no decision to record on it afterwards. Nothing here is in the enforcement path: the
proxy's `Active` condition is shown as written, never recomputed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from datetime import datetime
from uuid import UUID

from kubernetes_asyncio import client as k8s_client
from more_itertools import unique_everseen
from pydantic import BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient
from x.agentplane.app.inventory import InventoryError
from x.agentplane.egress.resources import (
    ACTIVE_CONDITION,
    ConditionStatus,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    Rule,
    SchemeTokenTarget,
    Target,
    TargetMethod,
)

# Flux stamps its inventory labels on everything it applies (cluster/k8s/agentplane-staging/egress);
# nothing at runtime deletes such a binding, since the next reconcile would apply it again.
FLUX_KUSTOMIZATION_LABEL = "kustomize.toolkit.fluxcd.io/name"

EGRESS_API = ("agentplane.allegedly.works", "v1alpha1")
POLICIES_PLURAL = "egresspolicies"
BINDINGS_PLURAL = "egressbindings"
CREDENTIALS_PLURAL = "egresscredentials"
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


class UnknownPolicyError(InventoryError):
    """A grant naming a policy the namespace does not hold, which would grant nothing.

    The CRD admits any string in `spec.policies` and the proxy answers a name that resolves to
    nothing with `MissingPolicy`, so a dangling name is a state the system already handles. This
    refuses one at the moment it would be written; one the operator deletes afterwards still lands
    there, and the proxy's condition stays the answer to it.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"no EgressPolicy in the namespace is named {', '.join(names)}")
        self.names = names


# The kinds themselves are `x.agentplane.egress.resources`, shared with the proxy that enforces them.
# The app modelled them separately until node H moved a rule's credential to `credentialRef` and the
# same edit had to be made twice; a second copy of a CRD's shape is a second thing to forget.


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ResourceList(_Wire):
    items: list[dict[str, object]]


# API views.


class CredentialTargetView(BaseModel):
    """One place the credential is substituted, as the credential declares it."""

    model_config = ConfigDict(extra="forbid")

    header: str
    method: TargetMethod
    scheme: str | None = Field(default=None, description="The scheme `schemeToken` expects; absent for the rest.")


class CredentialView(BaseModel):
    """A credential a rule lets a subject present. The value lives only in the proxy: this names the
    Secret it is drawn from, never its content."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(description="What the credential is and what it can do, as its owner wrote it.")
    placeholder: str = Field(description="What a sandbox sends in its place; derived from the name.")
    secret: str
    key: str = Field(description="Key of that Secret, in the proxy's credentials namespace.")
    targets: list[CredentialTargetView]


class RuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hosts: list[str]
    methods: list[str] | None = Field(default=None, description="Absent admits any method.")
    paths: list[str] | None = Field(default=None, description="Absent admits any path.")
    credential: CredentialView | None = Field(
        default=None, description="The EgressCredential this rule lets a subject present, when it resolves."
    )
    missing_credential: str | None = Field(
        default=None, description="Named by the rule and absent from the namespace, so the rule substitutes nothing."
    )


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

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient, default_policies: Sequence[str] = ()):
        self._namespace = namespace
        self._custom_objects = custom_objects
        self._default_policies = default_policies

    def launch_policies(self, picked: Sequence[str]) -> list[str]:
        """What a new sandbox is granted: the policies no sandbox works without, then what the caller
        picked. Picking a default again is not an error and does not name it twice."""
        return list(unique_everseen([*self._default_policies, *picked]))

    async def list_policies(self) -> list[PolicyView]:
        policies, credentials = await asyncio.gather(self._list(POLICIES_PLURAL), self._list(CREDENTIALS_PLURAL))
        return policy_views(
            _ResourceList.model_validate(policies).items, _ResourceList.model_validate(credentials).items
        )

    async def require_policies(self, names: list[str]) -> None:
        """Every name must resolve to a policy the namespace holds, or nothing is written."""
        _require_known(names, await self._policies_by_name())

    async def bindings_for(self, sandbox: str) -> list[BindingView]:
        """Every binding with a subject naming the sandbox, in name order."""
        policies, bindings, credentials = await asyncio.gather(
            self._list(POLICIES_PLURAL), self._list(BINDINGS_PLURAL), self._list(CREDENTIALS_PLURAL)
        )
        return matching_bindings(
            _ResourceList.model_validate(bindings).items,
            _ResourceList.model_validate(policies).items,
            _ResourceList.model_validate(credentials).items,
            sandbox=sandbox,
        )

    async def revoke(self, name: str) -> None:
        """Delete a runtime binding, which is how a grant is taken back; a Flux-applied one is
        refused, git being its owner."""
        if FLUX_KUSTOMIZATION_LABEL in (await self._binding(name)).metadata.labels:
            raise FluxOwnedBindingError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *EGRESS_API, self._namespace, BINDINGS_PLURAL, name, body=k8s_client.V1DeleteOptions()
        )

    async def grant(self, *, sandbox: str, sandbox_uid: UUID, policies: list[str]) -> BindingView:
        """One binding of the sandbox to the policies, owned by the Sandbox so its deletion
        garbage-collects it. Creating it is the grant, at launch and afterwards alike: granting an
        already-running sandbox adds another binding rather than editing one it has, so each grant's
        `expiresAt` is its own.
        """
        known = await self._policies_by_name()
        _require_known(policies, known)
        created = await self._custom_objects.create_namespaced_custom_object(
            *EGRESS_API,
            self._namespace,
            BINDINGS_PLURAL,
            {
                "apiVersion": "/".join(EGRESS_API),
                "kind": "EgressBinding",
                "metadata": {
                    # The API server names it. A sandbox may be granted more than once, and a name
                    # derived from the sandbox alone would make every grant after the first a 409.
                    "generateName": f"{sandbox}-",
                    # Not the controller: the Sandbox controller owns the Pod and PVC, and this
                    # reference is for cascading deletion only. It cascades only while bindings and
                    # Sandboxes share a namespace — Kubernetes treats a namespaced owner in another
                    # namespace as absent and collects the dependent — so splitting
                    # `--sandbox-namespace` off `--namespace` has to replace this with a sweep.
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
            },
        )
        return _binding_view(EgressBinding.model_validate(created), known)

    async def _policies_by_name(self) -> dict[str, PolicyView]:
        return {view.name: view for view in await self.list_policies()}

    async def _list(self, plural: str) -> dict[str, object]:
        return await self._custom_objects.list_namespaced_custom_object(*EGRESS_API, self._namespace, plural)

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


# The projections, over objects however they were obtained: one request's list, or the copy
# `live.py` keeps under a watch. Both go through here, so a pushed row and a fetched one are the
# same row.


def matching_bindings(
    bindings: Iterable[object], policies: Iterable[object], credentials: Iterable[object], *, sandbox: str
) -> list[BindingView]:
    """Every binding with a subject naming the sandbox, in name order."""
    known = _credentials_by_name(credentials)
    resolved = {
        policy.metadata.name: _policy_view(policy, known) for policy in map(EgressPolicy.model_validate, policies)
    }
    return sorted(
        (
            _binding_view(binding, resolved)
            for binding in map(EgressBinding.model_validate, bindings)
            if any(subject.sandbox.name == sandbox for subject in binding.spec.subjects)
        ),
        key=lambda view: view.name,
    )


def policy_views(policies: Iterable[object], credentials: Iterable[object]) -> list[PolicyView]:
    resolved = _credentials_by_name(credentials)
    return [_policy_view(EgressPolicy.model_validate(policy), resolved) for policy in policies]


def _credentials_by_name(credentials: Iterable[object]) -> dict[str, CredentialView]:
    return {
        credential.metadata.name: CredentialView(
            name=credential.metadata.name,
            description=credential.spec.description,
            placeholder=credential.placeholder,
            secret=credential.spec.source.secret_ref.name,
            key=credential.spec.source.secret_ref.key,
            targets=[_target_view(target) for target in credential.spec.targets],
        )
        for credential in map(EgressCredential.model_validate, credentials)
    }


def _target_view(target: Target) -> CredentialTargetView:
    return CredentialTargetView(
        header=target.header,
        method=target.method,
        scheme=target.scheme if isinstance(target, SchemeTokenTarget) else None,
    )


def _policy_view(policy: EgressPolicy, credentials: dict[str, CredentialView]) -> PolicyView:
    return PolicyView(name=policy.metadata.name, rules=[_rule_view(rule, credentials) for rule in policy.spec.rules])


def _rule_view(rule: Rule, credentials: dict[str, CredentialView]) -> RuleView:
    """A rule naming a credential the namespace does not hold reports the name it named: the proxy
    substitutes nothing for it, and an operator should see which name dangles rather than a rule
    that looks credential-less."""
    named = None if rule.credential_ref is None else rule.credential_ref.name
    return RuleView(
        hosts=rule.hosts,
        methods=rule.methods,
        paths=rule.paths,
        credential=None if named is None else credentials.get(named),
        missing_credential=named if named is not None and named not in credentials else None,
    )


def _binding_view(binding: EgressBinding, policies: dict[str, PolicyView]) -> BindingView:
    # No status at all until the proxy has written one, which is the same "not yet decided" the
    # absent condition below means.
    conditions = binding.status.conditions if binding.status is not None else []
    active = next((c for c in conditions if c.type == ACTIVE_CONDITION), None)
    return BindingView(
        name=binding.metadata.name,
        from_git=FLUX_KUSTOMIZATION_LABEL in binding.metadata.labels,
        subjects=[subject.sandbox.name for subject in binding.spec.subjects],
        expires_at=binding.spec.expires_at,
        policies=[policies[name] for name in binding.spec.policies if name in policies],
        missing_policies=[name for name in binding.spec.policies if name not in policies],
        active=None if active is None else active.status is ConditionStatus.TRUE,
        active_reason=active.reason if active is not None else None,
        active_message=active.message if active is not None else None,
    )
