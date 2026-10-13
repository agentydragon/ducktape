"""Public egress views and read-only Kubernetes projections. No grant mutation authority.

A policy here is a `PolicySet`'s `egress` rules and a binding a `PolicyBinding` naming such a set;
the other rule types of the same sets belong to other readers and show nowhere in these views.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.resources import EGRESS_RULE_TYPE, EgressCredential, Rule, Target
from agentplane.policy_sets import (
    POLICY_BINDINGS_PLURAL,
    POLICY_SETS_PLURAL,
    PolicyBinding,
    PolicySet,
    RefusedPolicySet,
    parse_policy_set,
    resolve_binding,
)
from agentplane.sandbox_resource import SANDBOX_KIND
from agentplane.sandbox_service.errors import InventoryError
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient

# Flux stamps its inventory labels on everything it applies (cluster/k8s/agentplane-staging);
# nothing at runtime deletes such a binding, since the next reconcile would apply it again.
FLUX_KUSTOMIZATION_LABEL = "kustomize.toolkit.fluxcd.io/name"

EGRESS_API = (GROUP, VERSION)
CREDENTIALS_PLURAL = "egresscredentials"


class BindingNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no PolicyBinding {name=}")
        self.name = name


class FluxOwnedBindingError(InventoryError):
    """A Flux-applied binding is git's to remove; deleting it at runtime would only be re-applied."""

    def __init__(self, name: str) -> None:
        super().__init__(f"PolicyBinding {name=} comes from git; remove it there")
        self.name = name


class SandboxOwnedBindingError(InventoryError):
    """The Sandbox controller keeps a Sandbox's own binding equal to its spec, so a delete would be undone."""

    def __init__(self, name: str) -> None:
        super().__init__(f"PolicyBinding {name=} is the Sandbox's own; change the Sandbox's policy sets instead")
        self.name = name


class UnknownPolicyError(InventoryError):
    """A grant naming a PolicySet with no egress rules in the namespace, which would grant nothing.

    The CRD admits any string in `spec.policySets`, and a name that resolves to nothing grants
    nothing, so a dangling name is a state the system already handles. This refuses one at the
    moment it would be written; one the operator deletes afterwards still lands there.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"no PolicySet with egress rules in the namespace is named {', '.join(names)}")
        self.names = names


# The rule and credential kinds are `agentplane.egress.resources`, shared with the proxy that enforces them.
# The app modelled them separately until node H moved a rule's credential to `credentialRef` and the
# same edit had to be made twice; a second copy of a CRD's shape is a second thing to forget.


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ResourceList(_Wire):
    items: list[dict[str, object]]


# API views.


class CredentialView(BaseModel):
    """A credential a rule lets a subject present. Source metadata names no credential value."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(description="What the credential is and what it can do, as its owner wrote it.")
    placeholder: str = Field(description="What a sandbox sends in its place; derived from the name.")
    secret: str | None = Field(default=None, description="Source Secret, absent for a workload-token source.")
    key: str | None = Field(default=None, description="Source Secret key, absent for a workload-token source.")
    targets: list[Target]


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
    subjects: list[ServiceAccountRef] = Field(description="The ServiceAccounts this binding names.")
    expires_at: datetime | None = None
    policy_sets: list[str] = Field(description="Every PolicySet the binding names, of any rule type, in its order.")
    policies: list[PolicyView] = Field(
        description="The named PolicySets that hold egress rules, in the binding's order."
    )
    missing_policies: list[str] = Field(
        description="PolicySet names in the binding that no usable PolicySet answers to: absent, or refused."
    )


class EgressReader:
    """Read-only Kubernetes projections, shared by the service and the app."""

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient) -> None:
        self._namespace = namespace
        self._custom_objects = custom_objects

    async def list_policies(self) -> list[PolicyView]:
        sets, credentials = await asyncio.gather(self._list(POLICY_SETS_PLURAL), self._list(CREDENTIALS_PLURAL))
        return policy_views(_ResourceList.model_validate(sets).items, _ResourceList.model_validate(credentials).items)

    async def bindings_for(self, subject: ServiceAccountRef) -> list[BindingView]:
        """Every binding naming this subject, in name order."""
        sets, bindings, credentials = await asyncio.gather(
            self._list(POLICY_SETS_PLURAL), self._list(POLICY_BINDINGS_PLURAL), self._list(CREDENTIALS_PLURAL)
        )
        return matching_bindings(
            _ResourceList.model_validate(bindings).items,
            _ResourceList.model_validate(sets).items,
            _ResourceList.model_validate(credentials).items,
            subject=subject,
        )

    async def _policies_by_name(self) -> dict[str, PolicyView]:
        return {view.name: view for view in await self.list_policies()}

    async def _list(self, plural: str) -> dict[str, object]:
        return await self._custom_objects.list_namespaced_custom_object(*EGRESS_API, self._namespace, plural)


# The projections, over objects however they were obtained: one request's list, or the copy
# `live.py` keeps under a watch. Both go through here, so a pushed row and a fetched one are the
# same row.


def _egress_rule(raw: dict[str, Any]) -> Rule | None:
    return Rule.model_validate(raw) if raw["type"] == EGRESS_RULE_TYPE else None


def egress_sets(policy_sets: Iterable[object]) -> dict[str, PolicySet[Rule] | RefusedPolicySet]:
    """Each set as the egress reader sees it: its `egress` rules, or refused when one does not parse."""
    parsed = [parse_policy_set(raw, parse_rule=_egress_rule) for raw in policy_sets]
    return {policy_set.name: policy_set for policy_set in parsed}


def matching_bindings(
    bindings: Iterable[object],
    policy_sets: Iterable[object],
    credentials: Iterable[object],
    *,
    subject: ServiceAccountRef,
) -> list[BindingView]:
    """Every binding naming this subject, in name order, but for the Sandbox's own: that is its spec's
    `policySets`, which the Sandbox's bound policy already shows, and it is not the app's to revoke."""
    sets = egress_sets(policy_sets)
    known = _credentials_by_name(credentials)
    return sorted(
        (
            binding_view(binding, sets, known)
            for binding in map(PolicyBinding.model_validate, bindings)
            if binding.spec.subject == subject and not is_sandbox_controlled(binding)
        ),
        key=lambda view: view.name,
    )


def policy_views(policy_sets: Iterable[object], credentials: Iterable[object]) -> list[PolicyView]:
    """The sets that hold egress rules."""
    resolved = _credentials_by_name(credentials)
    return [
        _policy_view(policy_set, resolved)
        for policy_set in egress_sets(policy_sets).values()
        if isinstance(policy_set, PolicySet) and policy_set.rules
    ]


def _credentials_by_name(credentials: Iterable[object]) -> dict[str, CredentialView]:
    resolved: dict[str, CredentialView] = {}
    for credential in map(EgressCredential.model_validate, credentials):
        secret_ref = credential.spec.source.secret_ref
        resolved[credential.metadata.name] = CredentialView(
            name=credential.metadata.name,
            description=credential.spec.description,
            placeholder=credential.placeholder,
            secret=secret_ref.name if secret_ref is not None else None,
            key=secret_ref.key if secret_ref is not None else None,
            targets=list(credential.spec.targets),
        )
    return resolved


def _policy_view(policy_set: PolicySet[Rule], credentials: dict[str, CredentialView]) -> PolicyView:
    return PolicyView(
        name=policy_set.name, rules=[_rule_view(numbered.rule, credentials) for numbered in policy_set.rules]
    )


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


def is_sandbox_controlled(binding: PolicyBinding) -> bool:
    return any(ref.controller and ref.kind == SANDBOX_KIND for ref in binding.metadata.owner_references)


def binding_view(
    binding: PolicyBinding,
    sets: Mapping[str, PolicySet[Rule] | RefusedPolicySet],
    credentials: dict[str, CredentialView],
) -> BindingView:
    resolution = resolve_binding(sets, binding)
    return BindingView(
        name=binding.metadata.name,
        from_git=FLUX_KUSTOMIZATION_LABEL in binding.metadata.labels,
        subjects=[binding.spec.subject],
        expires_at=binding.spec.expires_at,
        policy_sets=binding.spec.policy_sets,
        policies=[_policy_view(policy_set, credentials) for policy_set in resolution.sets if policy_set.rules],
        missing_policies=[*resolution.missing, *(refused.name for refused in resolution.refused)],
    )
