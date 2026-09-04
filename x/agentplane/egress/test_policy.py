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
    binding_status,
    evaluate,
    host_matches,
    path_matches,
)
from x.agentplane.egress.presentation import HeaderRewrite
from x.agentplane.egress.resources import (
    ActiveReason,
    BasicPasswordTarget,
    BasicUsernameTarget,
    BindingSpec,
    ConditionStatus,
    CredentialRef,
    CredentialSource,
    CredentialSpec,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    ObjectMeta,
    PolicySpec,
    Rule,
    Sandbox,
    SandboxRef,
    SchemeTokenTarget,
    Secret,
    SecretKeyRef,
    Subject,
    Target,
    TargetMethod,
    WholeValueTarget,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SECRET_VALUE = "real-value"
APP_SECRET_VALUE = "real-app-value"
SANDBOX = Sandbox(metadata=ObjectMeta(name="sb", uid="sb-uid"))
AUTHORIZATION = "Authorization"
BEARER = SchemeTokenTarget(header=AUTHORIZATION, method=TargetMethod.SCHEME_TOKEN, scheme="Bearer")
BASIC_PASSWORD = BasicPasswordTarget(header=AUTHORIZATION, method=TargetMethod.BASIC_PASSWORD)


def credential(name: str, *targets: Target, key: str = "token") -> EgressCredential:
    return EgressCredential(
        metadata=ObjectMeta(name=name, generation=1),
        spec=CredentialSpec(
            source=CredentialSource(secret_ref=SecretKeyRef(name="pat", key=key)),
            description=f"the {name} test credential",
            targets=list(targets),
        ),
    )


GITHUB_CREDENTIAL = credential("github-pat", BEARER, BASIC_PASSWORD)
PLACEHOLDER = GITHUB_CREDENTIAL.placeholder
APP_CREDENTIAL = credential("app-token", BEARER, key="app")
APP_PLACEHOLDER = APP_CREDENTIAL.placeholder
GITHUB_RULE = Rule(
    hosts=["api.github.com"],
    methods=["GET", "POST"],
    paths=["/repos/**"],
    credential_ref=CredentialRef(name=GITHUB_CREDENTIAL.metadata.name),
)
PUBLIC_RULE = Rule(hosts=["*.example.com"], paths=["/public/*"])
APP_RULE = Rule(
    hosts=["api.github.com"],
    methods=["GET", "POST"],
    paths=["/repos/**"],
    credential_ref=CredentialRef(name=APP_CREDENTIAL.metadata.name),
)
OPEN_RULE = Rule(hosts=["api.github.com"])


def policy(name: str, *rules: Rule) -> EgressPolicy:
    return EgressPolicy(metadata=ObjectMeta(name=name, generation=1), spec=PolicySpec(rules=list(rules)))


def binding(
    name: str, *, policies: list[str], subjects: list[Subject] | None = None, expires_at: datetime | None = None
) -> EgressBinding:
    return EgressBinding(
        metadata=ObjectMeta(name=name, generation=3),
        spec=BindingSpec(
            subjects=subjects if subjects is not None else [Subject(sandbox=SandboxRef(name="sb"))],
            policies=policies,
            expires_at=expires_at,
        ),
    )


def index(
    *,
    policies: list[EgressPolicy],
    bindings: list[EgressBinding],
    credentials: list[EgressCredential] | None = None,
    secret_value: str | None = SECRET_VALUE,
) -> Index:
    resolved = [GITHUB_CREDENTIAL, APP_CREDENTIAL] if credentials is None else credentials
    return Index(
        policies={p.metadata.name: p for p in policies},
        bindings={b.metadata.name: b for b in bindings},
        credentials={c.metadata.name: c for c in resolved},
        sandboxes={SANDBOX.metadata.name: SANDBOX},
        secrets=(
            {"pat": Secret(name="pat", data={"token": secret_value, "app": APP_SECRET_VALUE})}
            if secret_value is not None
            else {}
        ),
    )


def request(
    method: str = "GET", host: str = "api.github.com", path: str = "/repos/o/r", **headers: str
) -> EgressRequest:
    return EgressRequest(method=method, host=host, port=443, path=path, headers={k: [v] for k, v in headers.items()})


BASE_INDEX = index(policies=[policy("github", GITHUB_RULE, PUBLIC_RULE)], bindings=[binding("b", policies=["github"])])
SWAPPED = (HeaderRewrite(header=AUTHORIZATION, values=(f"Bearer {SECRET_VALUE}",)),)
APP_SWAPPED = (HeaderRewrite(header=AUTHORIZATION, values=(f"Bearer {APP_SECRET_VALUE}",)),)


def basic(payload: str) -> str:
    return f"Basic {base64.b64encode(payload.encode()).decode()}"


TWO_CREDENTIALS = index(
    policies=[policy("tokens", GITHUB_RULE, APP_RULE)], bindings=[binding("b", policies=["tokens"])]
)


def broad_and_credentialed(*bindings: EgressBinding) -> Index:
    """One host reachable two ways: a rule that carries the credential, and one that carries nothing."""
    return index(policies=[policy("open", OPEN_RULE), policy("github", GITHUB_RULE)], bindings=list(bindings))


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
        request(host="www.example.com", path="/public/x", authorization=basic(f"git:{PLACEHOLDER}")),
        Denied(DenyReason.PLACEHOLDER_UNRESOLVED),
    ),
    Case(
        "a placeholder that is only a substring of the component is not presented",
        BASE_INDEX,
        request(authorization=f"Bearer prefix-{PLACEHOLDER}-suffix"),
        Allowed("b", "github", 0),
    ),
    Case(
        "a placeholder under a scheme no target declares is not presented",
        BASE_INDEX,
        request(authorization=f"Token {PLACEHOLDER}"),
        Allowed("b", "github", 0),
    ),
    Case(
        "a placeholder in a header no target names is not presented",
        BASE_INDEX,
        request(**{"x-other": PLACEHOLDER}),
        Allowed("b", "github", 0),
    ),
    Case(
        "a basic payload with no colon is not the basicPassword target's shape",
        BASE_INDEX,
        request(authorization=basic(PLACEHOLDER)),
        Allowed("b", "github", 0),
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
            bindings=[binding("b", policies=["github"], subjects=[Subject(sandbox=SandboxRef(name="other"))])],
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
        "the credentialed rule decides though the broad binding sorts first",
        broad_and_credentialed(binding("a-open", policies=["open"]), binding("b-github", policies=["github"])),
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("b-github", "github", 0, SWAPPED),
    ),
    Case(
        "the credentialed rule decides though the broad binding sorts last",
        broad_and_credentialed(binding("a-github", policies=["github"]), binding("b-open", policies=["open"])),
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("a-github", "github", 0, SWAPPED),
    ),
    Case(
        "the credentialed rule decides though its policy is listed second",
        broad_and_credentialed(binding("b", policies=["open", "github"])),
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("b", "github", 0, SWAPPED),
    ),
    Case(
        "the credentialed rule decides though its policy is listed first",
        broad_and_credentialed(binding("b", policies=["github", "open"])),
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("b", "github", 0, SWAPPED),
    ),
    Case(
        "a request carrying no placeholder is allowed by the broad rule",
        broad_and_credentialed(binding("a-open", policies=["open"]), binding("b-github", policies=["github"])),
        request(method="DELETE", path="/user"),
        Allowed("a-open", "open", 0),
    ),
    Case(
        "a placeholder the subject is not bound to is denied",
        index(
            policies=[policy("github", GITHUB_RULE), policy("app", APP_RULE)],
            bindings=[binding("b", policies=["github"])],
        ),
        request(authorization=f"Bearer {APP_PLACEHOLDER}"),
        Denied(DenyReason.PLACEHOLDER_UNRESOLVED),
    ),
    Case(
        "the placeholder picks the first of two credentialed rules",
        TWO_CREDENTIALS,
        request(authorization=f"Bearer {PLACEHOLDER}"),
        Allowed("b", "tokens", 0, SWAPPED),
    ),
    Case(
        "the placeholder picks the second of two credentialed rules",
        TWO_CREDENTIALS,
        request(authorization=f"Bearer {APP_PLACEHOLDER}"),
        Allowed("b", "tokens", 1, APP_SWAPPED),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_evaluate(case: Case) -> None:
    assert evaluate(case.index, SANDBOX, case.request, NOW) == case.expected


def test_one_credential_is_substituted_at_whichever_target_the_request_uses() -> None:
    """The GitHub PAT is a bearer token to the API and a `Basic` password to git. Both targets are
    declared on the one credential, and each fires only where the request actually presents it."""
    bearer = evaluate(BASE_INDEX, SANDBOX, request(authorization=f"Bearer {PLACEHOLDER}"), NOW)
    assert bearer == Allowed("b", "github", 0, SWAPPED)
    git = evaluate(BASE_INDEX, SANDBOX, request(authorization=basic(f"x-access-token:{PLACEHOLDER}")), NOW)
    rewritten = (HeaderRewrite(header=AUTHORIZATION, values=(basic(f"x-access-token:{SECRET_VALUE}"),)),)
    assert git == Allowed("b", "github", 0, rewritten)


def test_a_basic_username_target_takes_the_half_before_the_first_colon() -> None:
    """What `https://<token>@github.com` sends. The placeholder carries no `:` -- it is derived, and
    the separator is `-` for exactly this reason -- so it can be a whole username component."""
    credentials = [
        credential("github-pat", BasicUsernameTarget(header=AUTHORIZATION, method=TargetMethod.BASIC_USERNAME))
    ]
    scoped = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], credentials=credentials
    )
    decision = evaluate(scoped, SANDBOX, request(authorization=basic(f"{PLACEHOLDER}:")), NOW)
    rewritten = (HeaderRewrite(header=AUTHORIZATION, values=(basic(f"{SECRET_VALUE}:"),)),)
    assert decision == Allowed("b", "github", 0, rewritten)


def test_a_whole_value_target_takes_the_header_entire() -> None:
    """The shape an API key travels in: `x-api-key: <key>`, no scheme to strip."""
    header = "X-Api-Key"
    credentials = [credential("github-pat", WholeValueTarget(header=header, method=TargetMethod.WHOLE_VALUE))]
    scoped = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], credentials=credentials
    )
    decision = evaluate(scoped, SANDBOX, request(**{"x-api-key": PLACEHOLDER}), NOW)
    assert decision == Allowed("b", "github", 0, (HeaderRewrite(header=header, values=(SECRET_VALUE,)),))


def test_substitution_covers_every_value_of_the_header() -> None:
    egress = EgressRequest(
        method="GET",
        host="api.github.com",
        port=443,
        path="/repos/x",
        headers={"authorization": [f"Bearer {PLACEHOLDER}", "Bearer other"]},
    )
    decision = evaluate(BASE_INDEX, SANDBOX, egress, NOW)
    rewritten = (HeaderRewrite(header=AUTHORIZATION, values=(f"Bearer {SECRET_VALUE}", "Bearer other")),)
    assert decision == Allowed("b", "github", 0, rewritten)


def test_a_rule_naming_a_credential_the_namespace_does_not_hold_forwards_untouched() -> None:
    """The ref dangles, so nothing is presented and nothing is substituted -- the rule's hosts,
    methods and paths still decide, which is what a request carrying no placeholder always gets."""
    scoped = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], credentials=[]
    )
    assert evaluate(scoped, SANDBOX, request(authorization=f"Bearer {PLACEHOLDER}"), NOW) == Allowed("b", "github", 0)


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
            binding("b", policies=["github"], expires_at=NOW),
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
    flipped = settled.model_copy(update={"spec": settled.spec.model_copy(update={"expires_at": NOW})})
    assert binding_status(BASE_INDEX, flipped, later).conditions[0].last_transition_time == later


if __name__ == "__main__":
    pytest_bazel.main()
