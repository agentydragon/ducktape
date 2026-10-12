"""The expansion of bindings and sets into native bindings, and each binding's verdict."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_bazel

from agentplane.kubernetes_policy.expansion import Expansion, Granted, NativeKey, Refusal, Refused, expand
from agentplane.policy.resources import (
    InvalidResource,
    PolicyBinding,
    PolicySet,
    RoleRef,
    parse_policy_binding,
    parse_policy_set,
)
from agentplane.subjects import ServiceAccountRef

POLICY_NAMESPACE = "test-policies"
SUBJECT_NAMESPACE = "test-subjects"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
DIAGNOSTICS = "test-diagnostics"
ACCOUNT = ServiceAccountRef(namespace=SUBJECT_NAMESPACE, name="test-account")
ACCOUNT_UID = "uid-test-account"


def policy_set(name: str, rules: list[dict[str, Any]]) -> PolicySet | InvalidResource:
    return parse_policy_set(
        {
            "metadata": {"name": name, "namespace": POLICY_NAMESPACE, "uid": f"uid-{name}", "generation": 1},
            "spec": {"rules": rules},
        }
    )


def binding(
    name: str, policy_sets: list[str], *, subject: ServiceAccountRef = ACCOUNT, expires_at: datetime | None = None
) -> PolicyBinding | InvalidResource:
    spec: dict[str, Any] = {"subject": subject.model_dump(), "policySets": policy_sets}
    if expires_at is not None:
        spec["expiresAt"] = expires_at.isoformat()
    return parse_policy_binding(
        {"metadata": {"name": name, "namespace": POLICY_NAMESPACE, "uid": f"uid-{name}", "generation": 1}, "spec": spec}
    )


DIAGNOSTICS_SET = policy_set(
    DIAGNOSTICS,
    [
        {
            "type": "kubernetes_role_binding",
            "role": {"kind": "ClusterRole", "name": "test-namespace-reader"},
            "namespaces": ["test-team-a", "test-team-b"],
        },
        {"type": "kubernetes_cluster_role_binding", "clusterRole": "test-node-reader"},
    ],
)
DIAGNOSTICS_SCOPES = {
    (RoleRef(kind="ClusterRole", name="test-namespace-reader"), "test-team-a"),
    (RoleRef(kind="ClusterRole", name="test-namespace-reader"), "test-team-b"),
    (RoleRef(kind="ClusterRole", name="test-node-reader"), None),
}


def run(
    bindings: dict[str, PolicyBinding | InvalidResource],
    *,
    policy_sets: dict[str, PolicySet | InvalidResource] | None = None,
    service_accounts: dict[ServiceAccountRef, str] | None = None,
) -> Expansion:
    return expand(
        policy_sets={DIAGNOSTICS: DIAGNOSTICS_SET} if policy_sets is None else policy_sets,
        bindings=bindings,
        service_accounts={ACCOUNT: ACCOUNT_UID} if service_accounts is None else service_accounts,
        subject_namespaces={SUBJECT_NAMESPACE},
        now=NOW,
    )


def scopes(expansion: Expansion, keys: frozenset[NativeKey]) -> set[tuple[RoleRef, str | None]]:
    return {(expansion.natives[key].role, expansion.natives[key].namespace) for key in keys}


def test_binding_grants_each_rule_in_each_namespace_to_its_subject() -> None:
    expansion = run({"grant": binding("grant", [DIAGNOSTICS])})
    verdict = expansion.verdicts["grant"]
    assert isinstance(verdict, Granted)
    assert scopes(expansion, verdict.natives) == DIAGNOSTICS_SCOPES
    assert {(native.subject, native.subject_uid) for native in expansion.natives.values()} == {(ACCOUNT, ACCOUNT_UID)}


def test_overlapping_bindings_keep_separate_natives_so_revoking_one_keeps_the_other() -> None:
    expansion = run({"first": binding("first", [DIAGNOSTICS]), "second": binding("second", [DIAGNOSTICS])})
    first, second = expansion.verdicts["first"], expansion.verdicts["second"]
    assert isinstance(first, Granted)
    assert isinstance(second, Granted)
    assert not first.natives & second.natives
    assert scopes(expansion, first.natives) == scopes(expansion, second.natives) == DIAGNOSTICS_SCOPES


def test_one_binding_naming_overlapping_sets_keeps_one_native_per_role_and_scope() -> None:
    expansion = run(
        {"grant": binding("grant", [DIAGNOSTICS, "copy"])},
        policy_sets={DIAGNOSTICS: DIAGNOSTICS_SET, "copy": DIAGNOSTICS_SET},
    )
    assert len(expansion.natives) == len(DIAGNOSTICS_SCOPES)


def test_recreated_account_gets_new_native_names() -> None:
    before = run({"grant": binding("grant", [DIAGNOSTICS])})
    after = run({"grant": binding("grant", [DIAGNOSTICS])}, service_accounts={ACCOUNT: "uid-recreated"})
    assert not before.natives.keys() & after.natives.keys()


def test_unavailable_sets_are_reported_and_the_rest_still_grant() -> None:
    invalid = policy_set("broken", [{"type": "kubernetes_cluster_role_binding"}])
    assert isinstance(invalid, InvalidResource)
    expansion = run(
        {"grant": binding("grant", ["absent", "broken", DIAGNOSTICS])},
        policy_sets={DIAGNOSTICS: DIAGNOSTICS_SET, "broken": invalid},
    )
    verdict = expansion.verdicts["grant"]
    assert isinstance(verdict, Granted)
    assert verdict.unavailable_sets == ("absent", "broken")
    assert scopes(expansion, verdict.natives) == DIAGNOSTICS_SCOPES


@pytest.mark.parametrize(
    ("grant", "refusal"),
    [
        (binding("grant", [DIAGNOSTICS], expires_at=NOW), Refusal.EXPIRED),
        (
            binding("grant", [DIAGNOSTICS], subject=ServiceAccountRef(namespace="test-elsewhere", name="test-account")),
            Refusal.SUBJECT_NOT_WATCHED,
        ),
        (
            binding("grant", [DIAGNOSTICS], subject=ServiceAccountRef(namespace=SUBJECT_NAMESPACE, name="test-absent")),
            Refusal.SUBJECT_MISSING,
        ),
        (binding("grant", []), Refusal.INVALID),
    ],
)
def test_refused_binding_grants_nothing(grant: PolicyBinding | InvalidResource, refusal: Refusal) -> None:
    expansion = run({"grant": grant})
    verdict = expansion.verdicts["grant"]
    assert isinstance(verdict, Refused)
    assert verdict.reason is refusal
    assert expansion.natives == {}


def test_next_expiry_is_the_earliest_unexpired_binding() -> None:
    soon, later = NOW + timedelta(minutes=5), NOW + timedelta(hours=1)
    expansion = run(
        {
            "soon": binding("soon", [DIAGNOSTICS], expires_at=soon),
            "later": binding("later", [DIAGNOSTICS], expires_at=later),
            "expired": binding("expired", [DIAGNOSTICS], expires_at=NOW - timedelta(minutes=1)),
            "open": binding("open", [DIAGNOSTICS]),
        }
    )
    assert expansion.next_expiry == soon


if __name__ == "__main__":
    pytest_bazel.main()
