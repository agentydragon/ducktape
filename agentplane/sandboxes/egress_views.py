"""Public views of a ServiceAccount's PolicyBindings and read-only Kubernetes projections. No grant
mutation authority.

A binding view carries every set the binding names with all of its rules, whichever reader enforces
them, and each reader's verdict from the binding's status. Only the egress rules are read into fields;
a rule of another type is shown as the set states it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.resources import EGRESS_RULE_TYPE, EgressCredential, Rule, Target
from agentplane.policy_sets import (
    POLICY_BINDINGS_PLURAL,
    POLICY_SETS_PLURAL,
    Condition,
    ConditionStatus,
    NumberedRule,
    PolicyBinding,
    PolicySet,
    RefusedPolicySet,
    parse_policy_set,
)
from agentplane.sandbox_resource import SANDBOX_KIND
from agentplane.sandboxes.errors import InventoryError
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


class EgressRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["egress"]
    number: int = Field(description="The rule's position among all of the set's rules, of every type.")
    hosts: list[str]
    methods: list[str] | None = Field(default=None, description="Absent admits any method.")
    paths: list[str] | None = Field(default=None, description="Absent admits any path.")
    credential: CredentialView | None = Field(
        default=None, description="The EgressCredential this rule lets a subject present, when it resolves."
    )
    missing_credential: str | None = Field(
        default=None, description="Named by the rule and absent from the namespace, so the rule substitutes nothing."
    )


class OtherRuleView(BaseModel):
    """A rule another reader enforces, as the set states it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["other"]
    number: int = Field(description="The rule's position among all of the set's rules, of every type.")
    type: str = Field(description="The rule type, which selects the reader that enforces it.")
    fields: dict[str, JsonValue] = Field(description="The rule's fields but its type.")


RuleView = Annotated[EgressRuleView | OtherRuleView, Field(discriminator="kind")]


class PolicySetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    generation: int
    rules: list[RuleView] = Field(description="Every rule of every type in the set's order; none when it is refused.")
    refused: str | None = Field(
        default=None,
        description="Why an egress rule does not parse, when one does not; the egress proxy then enforces nothing "
        "from the set. Another reader's refusal is in its condition on the binding.",
    )


class BindingProvenance(StrEnum):
    GIT = "git"
    SANDBOX = "sandbox"
    OPERATOR = "operator"


class ConditionView(BaseModel):
    """One reader's verdict on a binding, as it wrote it to the binding's status."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="Which reader judged: `EgressReady`, `ActionsReady` or `KubernetesReady`.")
    status: ConditionStatus
    reason: str
    message: str
    observed_generation: int | None = Field(
        default=None, description="The generation judged; behind the binding's while an edit is unjudged."
    )


class BindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provenance: BindingProvenance = Field(
        description="git: Flux applied it, so removing it is git's. sandbox: the Sandbox's own, which its "
        "controller keeps equal to the Sandbox's policy sets. operator: granted at runtime."
    )
    generation: int = Field(description="The binding's metadata generation; a verdict judged at an older one is stale.")
    subjects: list[ServiceAccountRef] = Field(description="The ServiceAccounts this binding names.")
    expires_at: datetime | None = None
    conditions: list[ConditionView] = Field(
        description="The verdict of each reader that has judged the binding, by type; none until one has."
    )
    policy_sets: list[PolicySetView] = Field(
        description="The named PolicySets that exist, refused ones included, in the binding's order."
    )
    missing_policy_sets: list[str] = Field(description="Names in the binding that no PolicySet answers to.")


class EgressReader:
    """Read-only Kubernetes projections, shared by the service and the app."""

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient) -> None:
        self._namespace = namespace
        self._custom_objects = custom_objects

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

    async def _list(self, plural: str) -> dict[str, object]:
        return await self._custom_objects.list_namespaced_custom_object(*EGRESS_API, self._namespace, plural)


# The projections, over objects however they were obtained: one request's list, or the copy
# `live.py` keeps under a watch. Both go through here, so a pushed row and a fetched one are the
# same row.


@dataclass(frozen=True)
class _OtherRule:
    type: str
    fields: dict[str, JsonValue]


def _parse_rule(raw: dict[str, Any]) -> Rule | _OtherRule:
    if raw["type"] == EGRESS_RULE_TYPE:
        return Rule.model_validate(raw)
    return _OtherRule(type=raw["type"], fields={key: value for key, value in raw.items() if key != "type"})


def matching_bindings(
    bindings: Iterable[object],
    policy_sets: Iterable[object],
    credentials: Iterable[object],
    *,
    subject: ServiceAccountRef,
) -> list[BindingView]:
    """Every binding naming this subject, in name order."""
    sets = _policy_set_views(policy_sets, _credentials_by_name(credentials))
    return sorted(
        (
            _binding_view(binding, sets)
            for binding in map(PolicyBinding.model_validate, bindings)
            if binding.spec.subject == subject
        ),
        key=lambda view: view.name,
    )


def _policy_set_views(
    policy_sets: Iterable[object], credentials: Mapping[str, CredentialView]
) -> dict[str, PolicySetView]:
    parsed: list[PolicySet[Rule | _OtherRule] | RefusedPolicySet] = [
        parse_policy_set(raw, parse_rule=_parse_rule) for raw in policy_sets
    ]
    return {view.name: view for view in (_policy_set_view(policy_set, credentials) for policy_set in parsed)}


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


def _policy_set_view(
    policy_set: PolicySet[Rule | _OtherRule] | RefusedPolicySet, credentials: Mapping[str, CredentialView]
) -> PolicySetView:
    assert policy_set.generation is not None, "sets are read off the API server"
    if isinstance(policy_set, RefusedPolicySet):
        return PolicySetView(
            name=policy_set.name, generation=policy_set.generation, rules=[], refused=policy_set.reason
        )
    return PolicySetView(
        name=policy_set.name,
        generation=policy_set.generation,
        rules=[_rule_view(numbered, credentials) for numbered in policy_set.rules],
    )


def _rule_view(numbered: NumberedRule[Rule | _OtherRule], credentials: Mapping[str, CredentialView]) -> RuleView:
    """A rule naming a credential the namespace does not hold reports the name it named: the proxy
    substitutes nothing for it, and an operator should see which name dangles rather than a rule
    that looks credential-less."""
    rule = numbered.rule
    if isinstance(rule, _OtherRule):
        return OtherRuleView(kind="other", number=numbered.number, type=rule.type, fields=rule.fields)
    named = None if rule.credential_ref is None else rule.credential_ref.name
    return EgressRuleView(
        kind="egress",
        number=numbered.number,
        hosts=rule.hosts,
        methods=rule.methods,
        paths=rule.paths,
        credential=None if named is None else credentials.get(named),
        missing_credential=named if named is not None and named not in credentials else None,
    )


def binding_provenance(binding: PolicyBinding) -> BindingProvenance:
    if FLUX_KUSTOMIZATION_LABEL in binding.metadata.labels:
        return BindingProvenance.GIT
    if any(ref.controller and ref.kind == SANDBOX_KIND for ref in binding.metadata.owner_references):
        return BindingProvenance.SANDBOX
    return BindingProvenance.OPERATOR


def _condition_view(condition: Condition) -> ConditionView:
    return ConditionView(
        type=condition.type,
        status=condition.status,
        reason=condition.reason,
        message=condition.message,
        observed_generation=condition.observed_generation,
    )


def _binding_view(binding: PolicyBinding, sets: Mapping[str, PolicySetView]) -> BindingView:
    names = binding.spec.policy_sets
    return BindingView(
        name=binding.metadata.name,
        provenance=binding_provenance(binding),
        generation=binding.metadata.generation,
        subjects=[binding.spec.subject],
        expires_at=binding.spec.expires_at,
        conditions=sorted(map(_condition_view, binding.status.conditions), key=lambda view: view.type),
        policy_sets=[sets[name] for name in names if name in sets],
        missing_policy_sets=[name for name in names if name not in sets],
    )
