"""The pure decision: bindings, policies, rules, globs, substitution, and the fail-closed edges."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel

from x.agentplane.egress.policy import (
    Allowed,
    Decision,
    Denied,
    DenyReason,
    EgressRequest,
    Index,
    Substitution,
    binding_status,
    evaluate,
    host_matches,
    path_matches,
)
from x.agentplane.egress.resources import (
    ActiveReason,
    Approval,
    ApprovalState,
    BindingSpec,
    ConditionStatus,
    Credential,
    EgressBinding,
    EgressPolicy,
    LabelSelector,
    NamedSubject,
    ObjectMeta,
    PolicySpec,
    Rule,
    Sandbox,
    SandboxRef,
    Secret,
    SecretKeyRef,
    SelectedSubjects,
    Subject,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
PLACEHOLDER = "PLACEHOLDER-TOKEN"
SECRET_VALUE = "real-value"
SANDBOX = Sandbox(metadata=ObjectMeta(name="sb", uid="sb-uid", labels={"team": "alpha"}))
CREDENTIAL = Credential(
    secret_ref=SecretKeyRef(name="pat", key="token"), header="Authorization", placeholder=PLACEHOLDER
)
GITHUB_RULE = Rule(hosts=["api.github.com"], methods=["GET", "POST"], paths=["/repos/**"], credential=CREDENTIAL)
PUBLIC_RULE = Rule(hosts=["*.example.com"], paths=["/public/*"])


def policy(name: str, *rules: Rule) -> EgressPolicy:
    return EgressPolicy(metadata=ObjectMeta(name=name, generation=1), spec=PolicySpec(rules=list(rules)))


def binding(
    name: str,
    *,
    policies: list[str],
    subjects: list[Subject] | None = None,
    approval: ApprovalState = ApprovalState.APPROVED,
    expires_at: datetime | None = None,
) -> EgressBinding:
    return EgressBinding(
        metadata=ObjectMeta(name=name, generation=3),
        spec=BindingSpec(
            subjects=subjects if subjects is not None else [NamedSubject(sandbox=SandboxRef(name="sb"))],
            policies=policies,
            approval=Approval(state=approval),
            expires_at=expires_at,
        ),
    )


def index(
    *, policies: list[EgressPolicy], bindings: list[EgressBinding], secret_value: str | None = SECRET_VALUE
) -> Index:
    return Index(
        policies={p.metadata.name: p for p in policies},
        bindings={b.metadata.name: b for b in bindings},
        sandboxes={SANDBOX.metadata.name: SANDBOX},
        secrets={"pat": Secret(name="pat", data={"token": secret_value})} if secret_value is not None else {},
    )


def request(
    method: str = "GET", host: str = "api.github.com", path: str = "/repos/o/r", **headers: str
) -> EgressRequest:
    return EgressRequest(method=method, host=host, port=443, path=path, headers={k: [v] for k, v in headers.items()})


BASE_INDEX = index(policies=[policy("github", GITHUB_RULE, PUBLIC_RULE)], bindings=[binding("b", policies=["github"])])
SWAPPED = Substitution(header="Authorization", values=(f"Bearer {SECRET_VALUE}",))


@dataclass(frozen=True)
class Case:
    name: str
    index: Index
    request: EgressRequest
    expected: Decision


CASES = [
    Case(
        "allow with substitution",
        BASE_INDEX,
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("b", "github", 0, SWAPPED),
    ),
    Case("allow without placeholder forwards as-is", BASE_INDEX, request(), Allowed("b", "github", 0)),
    Case(
        "allow second rule on wildcard host",
        BASE_INDEX,
        request(host="a.b.example.com", path="/public/x"),
        Allowed("b", "github", 1),
    ),
    Case(
        "connect decided on host alone",
        BASE_INDEX,
        EgressRequest(method="CONNECT", host="api.github.com", port=443),
        Allowed("b", "github", 0),
    ),
    Case("deny by method", BASE_INDEX, request(method="DELETE"), Denied(DenyReason.NO_RULE)),
    Case("deny by path", BASE_INDEX, request(path="/user"), Denied(DenyReason.NO_RULE)),
    Case("deny wildcard apex", BASE_INDEX, request(host="example.com", path="/public/x"), Denied(DenyReason.NO_RULE)),
    Case("deny unknown host", BASE_INDEX, request(host="evil.example.org"), Denied(DenyReason.NO_RULE)),
    Case(
        "deny connect unknown host",
        BASE_INDEX,
        EgressRequest(method="CONNECT", host="evil.example.org", port=443),
        Denied(DenyReason.NO_RULE),
    ),
    Case(
        "deny placeholder the matched rule does not substitute",
        BASE_INDEX,
        request(host="www.example.com", path="/public/x", authorization=f"Bearer {PLACEHOLDER}"),
        Denied(DenyReason.PLACEHOLDER_UNRESOLVED),
    ),
    Case(
        "deny placeholder inside basic payload unsubstituted",
        BASE_INDEX,
        request(
            host="www.example.com",
            path="/public/x",
            authorization="Basic " + base64.b64encode(f"git:{PLACEHOLDER}".encode()).decode(),
        ),
        Denied(DenyReason.PLACEHOLDER_UNRESOLVED),
    ),
    Case(
        "no binding",
        index(policies=[policy("github", GITHUB_RULE)], bindings=[]),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "binding for another sandbox",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[binding("b", policies=["github"], subjects=[NamedSubject(sandbox=SandboxRef(name="other"))])],
        ),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "selector binding matches labels",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[
                binding(
                    "b",
                    policies=["github"],
                    subjects=[SelectedSubjects(sandbox_selector=LabelSelector(match_labels={"team": "alpha"}))],
                )
            ],
        ),
        request(),
        Allowed("b", "github", 0),
    ),
    Case(
        "selector binding misses labels",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[
                binding(
                    "b",
                    policies=["github"],
                    subjects=[SelectedSubjects(sandbox_selector=LabelSelector(match_labels={"team": "beta"}))],
                )
            ],
        ),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "expired binding",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[binding("b", policies=["github"], expires_at=NOW - timedelta(seconds=1))],
        ),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "unexpired binding",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[binding("b", policies=["github"], expires_at=NOW + timedelta(hours=1))],
        ),
        request(),
        Allowed("b", "github", 0),
    ),
    Case(
        "pending binding",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[binding("b", policies=["github"], approval=ApprovalState.PENDING)],
        ),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "denied binding",
        index(
            policies=[policy("github", GITHUB_RULE)],
            bindings=[binding("b", policies=["github"], approval=ApprovalState.DENIED)],
        ),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "missing policy",
        index(policies=[], bindings=[binding("b", policies=["github"])]),
        request(),
        Denied(DenyReason.NO_BINDING),
    ),
    Case(
        "missing policy beside a resolved one still grants the resolved",
        index(policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["absent", "github"])]),
        request(),
        Allowed("b", "github", 0),
    ),
    Case(
        "credential secret missing denies rather than forwarding",
        index(
            policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], secret_value=None
        ),
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Denied(DenyReason.CREDENTIAL_UNAVAILABLE),
    ),
    Case(
        "first matching rule across bindings in name order wins",
        index(
            policies=[policy("open", Rule(hosts=["api.github.com"])), policy("github", GITHUB_RULE)],
            bindings=[binding("b-open", policies=["open"]), binding("a-github", policies=["github"])],
        ),
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("a-github", "github", 0, SWAPPED),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_evaluate(case: Case) -> None:
    assert evaluate(case.index, SANDBOX, case.request, NOW) == case.expected


def test_substitution_reaches_inside_basic_payload() -> None:
    """git over HTTPS sends `Basic base64(user:token)`; the swap re-encodes the payload."""
    presented = base64.b64encode(f"x-access-token:{PLACEHOLDER}".encode()).decode()
    decision = evaluate(BASE_INDEX, SANDBOX, request(authorization=f"Basic {presented}"), NOW)
    expected = base64.b64encode(f"x-access-token:{SECRET_VALUE}".encode()).decode()
    assert decision == Allowed("b", "github", 0, Substitution("Authorization", (f"Basic {expected}",)))


def test_substitution_covers_every_value_of_the_header() -> None:
    egress = EgressRequest(
        method="GET",
        host="api.github.com",
        port=443,
        path="/repos/x",
        headers={"authorization": [f"Bearer {PLACEHOLDER}", "Bearer other"]},
    )
    decision = evaluate(BASE_INDEX, SANDBOX, egress, NOW)
    assert decision == Allowed(
        "b", "github", 0, Substitution("Authorization", (f"Bearer {SECRET_VALUE}", "Bearer other"))
    )


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        ("api.github.com", "API.GitHub.com", True),
        ("*.github.com", "api.github.com", True),
        ("*.github.com", "a.b.github.com", True),
        ("*.github.com", "github.com", False),
        ("*.github.com", "evilgithub.com", False),
    ],
)
def test_host_matches(pattern: str, host: str, expected: bool) -> None:
    assert host_matches(pattern, host) is expected


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/repos/*/contents", "/repos/a/contents", True),
        ("/repos/*/contents", "/repos/a/b/contents", False),
        ("/repos/**", "/repos/a/b/c", True),
        ("/repos/**", "/repos", False),
        ("/repos/**/contents", "/repos/a/b/contents", True),
        ("/user", "/user?x=1", True),
        ("/user", "/users", False),
        ("/**", "/anything/at/all", True),
    ],
)
def test_path_matches(pattern: str, path: str, expected: bool) -> None:
    assert path_matches(pattern, path) is expected


@pytest.mark.parametrize(
    ("binding_", "policies", "status", "reason", "resolved"),
    [
        (
            binding("b", policies=["github"]),
            [policy("github", GITHUB_RULE)],
            ConditionStatus.TRUE,
            ActiveReason.RESOLVED,
            1,
        ),
        (
            binding("b", policies=["github", "absent"]),
            [policy("github", GITHUB_RULE)],
            ConditionStatus.TRUE,
            ActiveReason.RESOLVED,
            1,
        ),
        (binding("b", policies=["absent"]), [], ConditionStatus.FALSE, ActiveReason.MISSING_POLICY, 0),
        (
            binding("b", policies=["github"], approval=ApprovalState.PENDING),
            [policy("github", GITHUB_RULE)],
            ConditionStatus.FALSE,
            ActiveReason.NOT_APPROVED,
            1,
        ),
        (
            binding("b", policies=["github"], expires_at=NOW),
            [policy("github", GITHUB_RULE)],
            ConditionStatus.FALSE,
            ActiveReason.EXPIRED,
            1,
        ),
        # Expiry outranks approval: approving an expired binding changes nothing.
        (
            binding("b", policies=["github"], approval=ApprovalState.PENDING, expires_at=NOW),
            [policy("github", GITHUB_RULE)],
            ConditionStatus.FALSE,
            ActiveReason.EXPIRED,
            1,
        ),
    ],
)
def test_binding_status(
    binding_: EgressBinding, policies: list[EgressPolicy], status: ConditionStatus, reason: ActiveReason, resolved: int
) -> None:
    result = binding_status(index(policies=policies, bindings=[binding_]), binding_, NOW)
    assert (result.observed_generation, result.resolved_policies) == (3, resolved)
    (condition,) = result.conditions
    assert (condition.type, condition.status, condition.reason, condition.last_transition_time) == (
        "Active",
        status,
        reason,
        NOW,
    )


def test_binding_status_keeps_transition_time_while_status_holds() -> None:
    """Recomputing an unchanged status yields an equal value, so nothing is written twice."""
    first = binding_status(BASE_INDEX, BASE_INDEX.bindings["b"], NOW)
    settled = BASE_INDEX.bindings["b"].model_copy(update={"status": first})
    later = NOW + timedelta(minutes=5)
    assert binding_status(BASE_INDEX, settled, later) == first
    flipped = settled.model_copy(
        update={"spec": settled.spec.model_copy(update={"approval": Approval(state=ApprovalState.DENIED)})}
    )
    assert binding_status(BASE_INDEX, flipped, later).conditions[0].last_transition_time == later


if __name__ == "__main__":
    pytest_bazel.main()
