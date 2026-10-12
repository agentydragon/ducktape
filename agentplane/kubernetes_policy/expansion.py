"""What the Kubernetes rules of the watched PolicySets and PolicyBindings expand to: the native
RoleBindings and ClusterRoleBindings to keep, and what each PolicyBinding is told about its grant.

A pure function of the watched state and the time, so the controller's every pass recomputes the
whole picture and nothing it has not seen survives a restart.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

from agentplane.policy.resources import (
    InvalidResource,
    KubernetesClusterRoleBindingRule,
    KubernetesRoleBindingRule,
    PolicyBinding,
    PolicySet,
    RoleRef,
)
from agentplane.subjects import ServiceAccountRef


class NativeKey(NamedTuple):
    """Where a native binding lives: a RoleBinding by namespace and name, a ClusterRoleBinding by name."""

    namespace: str | None
    name: str


@dataclass(frozen=True)
class NativeBinding:
    """One RoleBinding, or a ClusterRoleBinding where `namespace` is None, granting `role` to `subject`."""

    name: str
    namespace: str | None
    role: RoleRef
    subject: ServiceAccountRef
    subject_uid: str
    binding_name: str
    binding_uid: str

    @property
    def key(self) -> NativeKey:
        return NativeKey(self.namespace, self.name)


class Refusal(StrEnum):
    """Why a PolicyBinding grants nothing; each is its condition's reason."""

    INVALID = "Invalid"
    EXPIRED = "Expired"
    SUBJECT_NOT_WATCHED = "SubjectNotWatched"
    SUBJECT_MISSING = "SubjectMissing"


@dataclass(frozen=True)
class Refused:
    reason: Refusal
    message: str


@dataclass(frozen=True)
class Granted:
    """The binding's native bindings, and the sets it names that are missing or invalid and so grant
    nothing; the rest of its sets still apply."""

    natives: frozenset[NativeKey]
    unavailable_sets: tuple[str, ...]


@dataclass(frozen=True)
class Expansion:
    natives: dict[NativeKey, NativeBinding]
    verdicts: dict[str, Refused | Granted]
    next_expiry: datetime | None


def native_name(binding: PolicyBinding, subject_uid: str, role: RoleRef, namespace: str | None) -> str:
    """Unique per binding, account, role and scope.

    Each binding keeps its own native bindings, so revoking one never takes away what another
    grants. The account's UID is in the name because RBAC matches a subject by name alone: a
    same-named account recreated later gets new native bindings, and the old ones are deleted.
    """
    digest = hashlib.sha256(
        f"{binding.metadata.uid}/{subject_uid}/{role.kind}/{role.name}/{namespace or ''}".encode()
    ).hexdigest()[:16]
    return f"pb-{binding.metadata.name[:40]}-{digest}"


def _scopes(rule: KubernetesRoleBindingRule | KubernetesClusterRoleBindingRule) -> list[tuple[RoleRef, str | None]]:
    if isinstance(rule, KubernetesRoleBindingRule):
        return [(rule.role, namespace) for namespace in sorted(rule.namespaces)]
    return [(RoleRef(kind="ClusterRole", name=rule.cluster_role), None)]


def _verdict(
    binding: PolicyBinding | InvalidResource,
    *,
    service_accounts: Mapping[ServiceAccountRef, str],
    subject_namespaces: Set[str],
    now: datetime,
) -> Refused | str:
    """The refusal, or the subject account's UID when the binding grants."""
    if isinstance(binding, InvalidResource):
        return Refused(Refusal.INVALID, binding.message)
    spec = binding.spec
    if spec.expires_at is not None and spec.expires_at <= now:
        return Refused(Refusal.EXPIRED, f"expired at {spec.expires_at.isoformat()}")
    if spec.subject.namespace not in subject_namespaces:
        return Refused(
            Refusal.SUBJECT_NOT_WATCHED, f"subject namespace {spec.subject.namespace} is not one this controller binds"
        )
    if (uid := service_accounts.get(spec.subject)) is None:
        return Refused(
            Refusal.SUBJECT_MISSING, f"ServiceAccount {spec.subject.namespace}/{spec.subject.name} not found"
        )
    return uid


def expand(
    *,
    policy_sets: Mapping[str, PolicySet | InvalidResource],
    bindings: Mapping[str, PolicyBinding | InvalidResource],
    service_accounts: Mapping[ServiceAccountRef, str],
    subject_namespaces: Set[str],
    now: datetime,
) -> Expansion:
    natives: dict[NativeKey, NativeBinding] = {}
    verdicts: dict[str, Refused | Granted] = {}
    expiries: list[datetime] = []
    for name, binding in bindings.items():
        verdict = _verdict(binding, service_accounts=service_accounts, subject_namespaces=subject_namespaces, now=now)
        if isinstance(verdict, Refused):
            verdicts[name] = verdict
            continue
        assert isinstance(binding, PolicyBinding)
        if binding.spec.expires_at is not None:
            expiries.append(binding.spec.expires_at)
        granted: set[NativeKey] = set()
        unavailable: list[str] = []
        for set_name in binding.spec.policy_sets:
            policy_set = policy_sets.get(set_name)
            if not isinstance(policy_set, PolicySet):
                unavailable.append(set_name)
                continue
            for rule in policy_set.spec.rules:
                for role, namespace in _scopes(rule):
                    native = NativeBinding(
                        name=native_name(binding, verdict, role, namespace),
                        namespace=namespace,
                        role=role,
                        subject=binding.spec.subject,
                        subject_uid=verdict,
                        binding_name=name,
                        binding_uid=binding.metadata.uid,
                    )
                    natives[native.key] = native
                    granted.add(native.key)
        verdicts[name] = Granted(frozenset(granted), tuple(unavailable))
    return Expansion(natives=natives, verdicts=verdicts, next_expiry=min(expiries, default=None))
