"""The pure decision: bindings, policies, rules, globs, substitution, and the fail-closed edges."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel

from agentplane.egress.policy import (
    Allowed,
    AuthenticatedWorkloadContext,
    Decision,
    Denied,
    DenyReason,
    EgressRequest,
    Index,
    evaluate,
    host_matches,
    path_matches,
    resolve_binding,
)
from agentplane.egress.presentation import HeaderRewrite
from agentplane.egress.resources import (
    ActiveReason,
    AuthenticatedWorkloadTokenSource,
    BasicPasswordTarget,
    BasicUsernameTarget,
    BindingSpec,
    CredentialRef,
    CredentialSource,
    CredentialSpec,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    ObjectMeta,
    PolicySpec,
    ProjectedWorkloadTokenSource,
    Rule,
    SchemeTokenTarget,
    Secret,
    SecretKeyRef,
    Target,
    TargetMethod,
    WholeValueTarget,
)
from agentplane.subjects import ServiceAccountRef

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SECRET_VALUE = "real-value"
APP_SECRET_VALUE = "real-app-value"
NAMESPACE = "agentplane-test"
CALLER = ServiceAccountRef(namespace=NAMESPACE, name="sb")
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


def workload_credential(name: str, *targets: Target) -> EgressCredential:
    return EgressCredential(
        metadata=ObjectMeta(name=name, generation=1),
        spec=CredentialSpec(
            source=CredentialSource(authenticated_workload_token=AuthenticatedWorkloadTokenSource()),
            description=f"the {name} calling workload",
            targets=list(targets),
        ),
    )


KUBERNETES_AUDIENCE = "https://kubernetes.test.invalid"


def projected_credential(name: str, *targets: Target, audience: str = KUBERNETES_AUDIENCE) -> EgressCredential:
    return EgressCredential(
        metadata=ObjectMeta(name=name, generation=1),
        spec=CredentialSpec(
            source=CredentialSource(projected_workload_token=ProjectedWorkloadTokenSource(audience=audience)),
            description=f"the {name} projected identity",
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
    name: str,
    *,
    policies: list[str],
    subjects: list[ServiceAccountRef] | None = None,
    expires_at: datetime | None = None,
) -> EgressBinding:
    return EgressBinding(
        metadata=ObjectMeta(name=name, generation=3),
        spec=BindingSpec(
            subjects=subjects if subjects is not None else [CALLER], policies=policies, expires_at=expires_at
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
            bindings=[
                binding("b", policies=["github"], subjects=[ServiceAccountRef(namespace=NAMESPACE, name="other")])
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


def test_a_binding_names_a_service_account_subject() -> None:
    """A workload no Sandbox owns is bound by the ServiceAccount its Pod runs as."""
    scoped = index(
        policies=[policy("github", GITHUB_RULE)],
        bindings=[
            binding(
                "b", policies=["github"], subjects=[ServiceAccountRef(namespace=NAMESPACE, name="test-workload-sa")]
            )
        ],
    )
    allowed = evaluate(scoped, ServiceAccountRef(namespace=NAMESPACE, name="test-workload-sa"), request(), NOW)
    assert isinstance(allowed, Allowed)


def test_a_service_account_binding_does_not_admit_another_service_account() -> None:
    scoped = index(
        policies=[policy("github", GITHUB_RULE)],
        bindings=[
            binding(
                "b", policies=["github"], subjects=[ServiceAccountRef(namespace=NAMESPACE, name="test-workload-sa")]
            )
        ],
    )
    assert evaluate(scoped, ServiceAccountRef(namespace=NAMESPACE, name="someone-else"), request(), NOW) == Denied(
        DenyReason.NO_BINDING
    )


def test_the_same_name_in_another_namespace_is_a_different_subject() -> None:
    """A subject is a namespace and a name together. The proxy serves workloads from more than one
    namespace, so a binding that matched on the name alone would reach across them."""
    bound = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"], subjects=[CALLER])]
    )
    elsewhere = ServiceAccountRef(namespace="somewhere-else", name=CALLER.name)

    assert evaluate(bound, CALLER, request(), NOW) == Allowed("b", "github", 0)
    assert evaluate(bound, elsewhere, request(), NOW) == Denied(DenyReason.NO_BINDING)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_evaluate(case: Case) -> None:
    assert evaluate(case.index, CALLER, case.request, NOW) == case.expected


def test_one_credential_is_substituted_at_whichever_target_the_request_uses() -> None:
    """The GitHub PAT is a bearer token to the API and a `Basic` password to git. Both targets are
    declared on the one credential, and each fires only where the request actually presents it."""
    bearer = evaluate(BASE_INDEX, CALLER, request(authorization=f"Bearer {PLACEHOLDER}"), NOW)
    assert bearer == Allowed("b", "github", 0, SWAPPED)
    git = evaluate(BASE_INDEX, CALLER, request(authorization=basic(f"x-access-token:{PLACEHOLDER}")), NOW)
    rewritten = (HeaderRewrite(header=AUTHORIZATION, values=(basic(f"x-access-token:{SECRET_VALUE}"),)),)
    assert git == Allowed("b", "github", 0, rewritten)


def test_authenticated_workload_source_substitutes_only_the_validated_context_bearer() -> None:
    dynamic = workload_credential("agentplane-workload", BEARER)
    dynamic_rule = GITHUB_RULE.model_copy(update={"credential_ref": CredentialRef(name=dynamic.metadata.name)})
    scoped = index(
        policies=[policy("workload", dynamic_rule)],
        bindings=[binding("b", policies=["workload"])],
        credentials=[dynamic],
    )
    token = "pod-a-authenticated-workload-token"
    decision = evaluate(
        scoped,
        CALLER,
        request(authorization=f"Bearer {dynamic.placeholder}"),
        NOW,
        authenticated_workload=AuthenticatedWorkloadContext(bearer=token, caller=CALLER, pod_uid="pod-a-uid"),
    )
    assert decision == Allowed("b", "workload", 0, (HeaderRewrite(header=AUTHORIZATION, values=(f"Bearer {token}",)),))
    assert token not in repr(decision)


def test_authenticated_workload_source_fails_without_authenticated_context() -> None:
    dynamic = workload_credential("agentplane-workload", BEARER)
    dynamic_rule = GITHUB_RULE.model_copy(update={"credential_ref": CredentialRef(name=dynamic.metadata.name)})
    scoped = index(
        policies=[policy("workload", dynamic_rule)],
        bindings=[binding("b", policies=["workload"])],
        credentials=[dynamic],
    )
    egress = request(authorization=f"Bearer {dynamic.placeholder}")
    assert evaluate(scoped, CALLER, egress, NOW) == Denied(DenyReason.CREDENTIAL_UNAVAILABLE)
    stale = AuthenticatedWorkloadContext(
        bearer="stale-token",
        caller=ServiceAccountRef(namespace=NAMESPACE, name="someone-else"),
        pod_uid="an-old-pod-uid",
    )
    assert evaluate(scoped, CALLER, egress, NOW, authenticated_workload=stale) == Denied(
        DenyReason.CREDENTIAL_UNAVAILABLE
    )


def _projected_index(credential_: EgressCredential) -> Index:
    rule = GITHUB_RULE.model_copy(update={"credential_ref": CredentialRef(name=credential_.metadata.name)})
    return index(
        policies=[policy("projected", rule)], bindings=[binding("b", policies=["projected"])], credentials=[credential_]
    )


def test_projected_workload_source_substitutes_the_token_for_its_own_audience() -> None:
    """The hop bearer is not what a destination validating its own audience will take, so a rule
    naming an audience gets the token presented for that audience and no other."""
    projected = projected_credential("kubernetes-workload", BEARER)
    api_server_token = "pod-a-api-server-token"
    context = AuthenticatedWorkloadContext(
        bearer="pod-a-hop-token",
        caller=CALLER,
        pod_uid="pod-a-uid",
        projected={KUBERNETES_AUDIENCE: api_server_token, "https://other.test.invalid": "not-this-one"},
    )
    decision = evaluate(
        _projected_index(projected),
        CALLER,
        request(authorization=f"Bearer {projected.placeholder}"),
        NOW,
        authenticated_workload=context,
    )
    assert decision == Allowed(
        "b", "projected", 0, (HeaderRewrite(header=AUTHORIZATION, values=(f"Bearer {api_server_token}",)),)
    )
    assert api_server_token not in repr(decision)
    assert context.bearer not in repr(decision)


def test_projected_workload_source_fails_when_its_audience_did_not_survive_review() -> None:
    """A token the verifier dropped is absent rather than present and unusable, so the rule naming
    it denies where it is used instead of spending some other audience's token."""
    projected = projected_credential("kubernetes-workload", BEARER)
    scoped = _projected_index(projected)
    egress = request(authorization=f"Bearer {projected.placeholder}")
    assert evaluate(scoped, CALLER, egress, NOW) == Denied(DenyReason.CREDENTIAL_UNAVAILABLE)
    without = AuthenticatedWorkloadContext(bearer="hop", caller=CALLER, pod_uid="pod-a-uid", projected={})
    assert evaluate(scoped, CALLER, egress, NOW, authenticated_workload=without) == Denied(
        DenyReason.CREDENTIAL_UNAVAILABLE
    )


def test_projected_workload_source_is_unusable_by_another_caller() -> None:
    """Context belongs to the caller it authenticated as: one subject's projected tokens are never
    substituted into another's request, however well-formed they are."""
    projected = projected_credential("kubernetes-workload", BEARER)
    someone_else = AuthenticatedWorkloadContext(
        bearer="hop",
        caller=ServiceAccountRef(namespace=NAMESPACE, name="someone-else"),
        pod_uid="pod-b-uid",
        projected={KUBERNETES_AUDIENCE: "pod-b-api-server-token"},
    )
    decision = evaluate(
        _projected_index(projected),
        CALLER,
        request(authorization=f"Bearer {projected.placeholder}"),
        NOW,
        authenticated_workload=someone_else,
    )
    assert decision == Denied(DenyReason.CREDENTIAL_UNAVAILABLE)


def test_credential_source_requires_exactly_one_known_tag() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CredentialSource()
    with pytest.raises(ValueError, match="exactly one"):
        CredentialSource(
            secret_ref=SecretKeyRef(name="pat", key="token"),
            authenticated_workload_token=AuthenticatedWorkloadTokenSource(),
        )
    with pytest.raises(ValueError, match="exactly one"):
        CredentialSource(
            authenticated_workload_token=AuthenticatedWorkloadTokenSource(),
            projected_workload_token=ProjectedWorkloadTokenSource(audience=KUBERNETES_AUDIENCE),
        )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        CredentialSource.model_validate({"forgedSource": {}})


def test_a_basic_username_target_takes_the_half_before_the_first_colon() -> None:
    """What `https://<token>@github.com` sends. The placeholder carries no `:` -- it is derived, and
    the separator is `-` for exactly this reason -- so it can be a whole username component."""
    credentials = [
        credential("github-pat", BasicUsernameTarget(header=AUTHORIZATION, method=TargetMethod.BASIC_USERNAME))
    ]
    scoped = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], credentials=credentials
    )
    decision = evaluate(scoped, CALLER, request(authorization=basic(f"{PLACEHOLDER}:")), NOW)
    rewritten = (HeaderRewrite(header=AUTHORIZATION, values=(basic(f"{SECRET_VALUE}:"),)),)
    assert decision == Allowed("b", "github", 0, rewritten)


def test_a_whole_value_target_takes_the_header_entire() -> None:
    """The shape an API key travels in: `x-api-key: <key>`, no scheme to strip."""
    header = "X-Api-Key"
    credentials = [credential("github-pat", WholeValueTarget(header=header, method=TargetMethod.WHOLE_VALUE))]
    scoped = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], credentials=credentials
    )
    decision = evaluate(scoped, CALLER, request(**{"x-api-key": PLACEHOLDER}), NOW)
    assert decision == Allowed("b", "github", 0, (HeaderRewrite(header=header, values=(SECRET_VALUE,)),))


def test_substitution_covers_every_value_of_the_header() -> None:
    egress = EgressRequest(
        method="GET",
        host="api.github.com",
        port=443,
        path="/repos/x",
        headers={"authorization": [f"Bearer {PLACEHOLDER}", "Bearer other"]},
    )
    decision = evaluate(BASE_INDEX, CALLER, egress, NOW)
    rewritten = (HeaderRewrite(header=AUTHORIZATION, values=(f"Bearer {SECRET_VALUE}", "Bearer other")),)
    assert decision == Allowed("b", "github", 0, rewritten)


def test_a_rule_naming_a_credential_the_namespace_does_not_hold_forwards_untouched() -> None:
    """The ref dangles, so nothing is presented and nothing is substituted -- the rule's hosts,
    methods and paths still decide, which is what a request carrying no placeholder always gets."""
    scoped = index(
        policies=[policy("github", GITHUB_RULE)], bindings=[binding("b", policies=["github"])], credentials=[]
    )
    assert evaluate(scoped, CALLER, request(authorization=f"Bearer {PLACEHOLDER}"), NOW) == Allowed("b", "github", 0)


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
        (binding("b", policies=["github"]), [policy("github", GITHUB_RULE)], True, ActiveReason.RESOLVED, 1),
        (binding("b", policies=["github", "absent"]), [policy("github", GITHUB_RULE)], True, ActiveReason.RESOLVED, 1),
        (binding("b", policies=["absent"]), [], False, ActiveReason.MISSING_POLICY, 0),
        (
            binding("b", policies=["github"], expires_at=NOW),
            [policy("github", GITHUB_RULE)],
            False,
            ActiveReason.EXPIRED,
            1,
        ),
    ],
)
def test_binding_resolution(
    binding_: EgressBinding, policies: list[EgressPolicy], status: bool, reason: ActiveReason, resolved: int
) -> None:
    result = resolve_binding(index(policies=policies, bindings=[binding_]), binding_, NOW)
    assert (result.active, result.reason, len(result.policies)) == (status, reason, resolved)


if __name__ == "__main__":
    pytest_bazel.main()
