"""Tests for the Loki read proxy, focused on the security-load-bearing validator."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from cluster.proxies.loki_read_proxy.main import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SELF_SUBJECT_ACCESS_REVIEW_PATH,
    UPSTREAM_TIMEOUT_S,
    Settings,
    create_app,
)

ALLOWLIST = frozenset({"flux-system", "monitoring", "kube-system"})

QUERY_RANGE = "/loki/api/v1/query_range"
QUERY_INSTANT = "/loki/api/v1/query"


# Tokens the fake apiserver authenticates, mapped to the namespaces their
# subject may `get pods/log` in. Deliberately disjoint from ALLOWLIST where it
# matters: RBAC must be the only thing a token-bearing request is judged by.
TOKENS = {
    "sa-token-rbac-monitoring": frozenset({"monitoring"}),
    "sa-token-rbac-matrix": frozenset({"matrix", "monitoring"}),
    "sa-token-rbac-nothing": frozenset[str](),
}
SETTINGS = Settings(
    upstream_url="http://loki-gateway.test",
    namespace_allowlist=ALLOWLIST,
    kube_api_url="https://kube-apiserver.test",
    kube_ca_file=None,
)


class Upstream:
    """MockTransport handler recording every request it receives."""

    def __init__(self, status_code: int = 200, content: bytes = b'{"status":"success"}') -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = status_code
        self.content = content

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, content=self.content, headers={"content-type": "application/json"})


class KubeApiserver:
    """MockTransport handler answering SelfSubjectAccessReviews from TOKENS."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "POST"
        assert request.url.path == SELF_SUBJECT_ACCESS_REVIEW_PATH
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token not in TOKENS:
            return httpx.Response(401, json={"kind": "Status", "message": "Unauthorized"})
        body = json.loads(request.content)
        attrs = body["spec"]["resourceAttributes"]
        allowed = attrs["namespace"] in TOKENS[token]
        status = {"allowed": allowed} | ({} if allowed else {"reason": "RBAC: no RoleBinding"})
        return httpx.Response(201, json=body | {"status": status})

    def reviewed_namespaces(self) -> list[str]:
        return [json.loads(r.content)["spec"]["resourceAttributes"]["namespace"] for r in self.requests]


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def kube() -> KubeApiserver:
    return KubeApiserver()


@pytest.fixture
def client(upstream: Upstream, kube: KubeApiserver) -> Iterator[TestClient]:
    app = create_app(SETTINGS, transport=httpx.MockTransport(upstream), kube_transport=httpx.MockTransport(kube))
    with TestClient(app) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _forwarded_params(upstream: Upstream) -> dict[str, list[str]]:
    (request,) = upstream.requests
    params = request.url.params
    return {key: params.get_list(key) for key in params}


def test_healthz(client: TestClient, upstream: Upstream) -> None:
    assert client.get("/healthz").status_code == 200
    assert upstream.requests == []


def test_plain_query_accepted(client: TestClient, upstream: Upstream) -> None:
    query = '{namespace="monitoring"}'
    resp = client.get(QUERY_RANGE, params={"query": query})
    assert resp.status_code == 200
    params = _forwarded_params(upstream)
    assert params["query"] == [query]
    assert params["limit"] == [str(DEFAULT_LIMIT)]


@pytest.mark.parametrize(
    "query",
    [
        # Pipeline stages, including braces inside quoted strings.
        '{namespace="flux-system", app="kustomize-controller"} |= "error" | line_format "{{.msg}}"',
        # Braces inside a quoted label value in the selector itself.
        '{namespace="monitoring", pod="a}{b"}',
        # Regex quantifier braces inside a line-filter string.
        '{namespace="monitoring"} |~ "a{3}b"',
        # Single-quoted and backtick (raw) string forms for the value.
        "{namespace='monitoring'}",
        '{namespace=`monitoring`, app="loki"}',
        # Leading whitespace and trailing comma are fine.
        '  \t{namespace="kube-system",}',
        # Additional matchers (even on namespace) are ANDed — narrowing only.
        '{namespace="monitoring", namespace="flux-system"}',
    ],
)
def test_accepted_queries(client: TestClient, upstream: Upstream, query: str) -> None:
    assert client.get(QUERY_RANGE, params={"query": query}).status_code == 200
    assert _forwarded_params(upstream)["query"] == [query]


@pytest.mark.parametrize(
    "query",
    [
        # Metric queries — count oracles over forbidden namespaces.
        'sum(rate({namespace="monitoring"}[1m]))',
        'count_over_time({namespace="matrix"}[5m])',
        # Missing / non-exact / non-allowlisted namespace matcher.
        '{app="foo"}',
        "{}",
        '{namespace=~"monitoring"}',
        '{namespace=~".*"} |= "x"',
        '{namespace!="monitoring"}',
        # Regex matcher alongside an exact one is still rejected.
        '{namespace="monitoring", namespace=~".*"}',
        # Values that can name no Kubernetes namespace: an empty one would ask
        # RBAC about "all namespaces" while matching Loki streams that carry no
        # namespace label at all.
        '{namespace=""}',
        '{namespace="Monitoring"}',
        '{namespace="monitoring "}',
        '{namespace="a.b"}',
        # Second selector attempts (invalid LogQL — must not pass regardless).
        '{namespace="flux-system"} or {namespace="matrix"}',
        '{namespace="flux-system"}{namespace="matrix"}',
        '{namespace="monitoring"} | label_format x={namespace="matrix"}',
        # Escapes must not smuggle a namespace past the raw-value grammar check.
        '{namespace="fl\\165x-system"}',
        # Malformed selectors.
        '{namespace="monitoring"',
        '{namespace="monitoring}',
        "{namespace=monitoring}",
        '{namespace == "monitoring"}',
        "",
        "   ",
    ],
)
def test_rejected_queries(client: TestClient, upstream: Upstream, kube: KubeApiserver, query: str) -> None:
    # Structural rejection happens before any authorization, so a token does
    # not turn a malformed query into an apiserver round trip either.
    for headers in ({}, _bearer("sa-token-rbac-matrix")):
        resp = client.get(QUERY_RANGE, params={"query": query}, headers=headers)
        assert resp.status_code == 400
    assert upstream.requests == []
    assert kube.requests == []


def test_anonymous_request_outside_allowlist_is_403(
    client: TestClient, upstream: Upstream, kube: KubeApiserver
) -> None:
    resp = client.get(QUERY_RANGE, params={"query": '{namespace="matrix"}'})
    assert resp.status_code == 403
    assert "bearer token" in resp.json()["detail"]
    assert upstream.requests == []
    assert kube.requests == []


def test_bearer_authorized_by_rbac_not_allowlist(client: TestClient, upstream: Upstream, kube: KubeApiserver) -> None:
    # matrix is outside the static allowlist; the token's RBAC admits it.
    query = '{namespace="matrix"} |= "oom"'
    resp = client.get(QUERY_RANGE, params={"query": query}, headers=_bearer("sa-token-rbac-matrix"))
    assert resp.status_code == 200
    assert kube.reviewed_namespaces() == ["matrix"]
    (review,) = kube.requests
    assert json.loads(review.content)["spec"]["resourceAttributes"] == {
        "group": "",
        "namespace": "matrix",
        "resource": "pods",
        "subresource": "log",
        "verb": "get",
    }
    (loki_request,) = upstream.requests
    assert _forwarded_params(upstream)["query"] == [query]
    # The caller's credential never travels to Loki.
    assert "authorization" not in loki_request.headers


def test_bearer_denied_by_rbac_even_inside_allowlist(
    client: TestClient, upstream: Upstream, kube: KubeApiserver
) -> None:
    # flux-system is allowlisted for anonymous callers, but a caller who
    # presented a token is judged by that token alone.
    resp = client.get(
        QUERY_RANGE, params={"query": '{namespace="flux-system"}'}, headers=_bearer("sa-token-rbac-nothing")
    )
    assert resp.status_code == 403
    assert "flux-system" in resp.json()["detail"]
    assert "RBAC: no RoleBinding" in resp.json()["detail"]
    assert kube.reviewed_namespaces() == ["flux-system"]
    assert upstream.requests == []


def test_bearer_every_pinned_namespace_reviewed(client: TestClient, upstream: Upstream, kube: KubeApiserver) -> None:
    query = '{namespace="monitoring", namespace="matrix"}'
    assert client.get(QUERY_RANGE, params={"query": query}, headers=_bearer("sa-token-rbac-matrix")).status_code == 200
    assert sorted(kube.reviewed_namespaces()) == ["matrix", "monitoring"]
    assert len(upstream.requests) == 1

    kube.requests.clear()
    upstream.requests.clear()
    resp = client.get(QUERY_RANGE, params={"query": query}, headers=_bearer("sa-token-rbac-monitoring"))
    assert resp.status_code == 403
    assert "matrix" in resp.json()["detail"]
    assert upstream.requests == []


def test_bearer_rejected_by_apiserver_is_401(client: TestClient, upstream: Upstream, kube: KubeApiserver) -> None:
    resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}'}, headers=_bearer("forged-token"))
    assert resp.status_code == 401
    assert len(kube.requests) == 1
    assert upstream.requests == []


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "sa-token-rbac-monitoring"])
def test_malformed_authorization_is_401_not_anonymous(
    client: TestClient, upstream: Upstream, kube: KubeApiserver, authorization: str
) -> None:
    # A caller who tried to authenticate must not fall through to the
    # anonymous allowlist (monitoring is allowlisted).
    resp = client.get(
        QUERY_RANGE, params={"query": '{namespace="monitoring"}'}, headers={"Authorization": authorization}
    )
    assert resp.status_code == 401
    assert kube.requests == []
    assert upstream.requests == []


def _client_with_kube(handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
    app = create_app(SETTINGS, transport=httpx.MockTransport(Upstream()), kube_transport=httpx.MockTransport(handler))
    return TestClient(app)


@pytest.mark.parametrize(
    "response",
    [
        # Fail closed on every non-decision: transport failure, timeout, an
        # apiserver error, an evaluation error, and an incomplete body.
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("timed out"),
        httpx.Response(500, json={"kind": "Status", "message": "etcd down"}),
        httpx.Response(201, json={"status": {"allowed": True, "evaluationError": "webhook authorizer failed"}}),
        httpx.Response(201, json={"status": {}}),
        httpx.Response(201, json={}),
    ],
)
def test_bearer_apiserver_non_decision_is_502(response: httpx.Response | Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    with _client_with_kube(handler) as client:
        resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}'}, headers=_bearer("any"))
    assert resp.status_code == 502


def test_bearer_denied_flag_wins_over_allowed() -> None:
    # `denied: true` alongside `allowed: true` is contradictory; never treat it as access.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": {"allowed": True, "denied": True}})

    with _client_with_kube(handler) as client:
        resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}'}, headers=_bearer("any"))
    assert resp.status_code == 403


def test_instant_query_endpoint_validates_too(client: TestClient, upstream: Upstream) -> None:
    assert client.get(QUERY_INSTANT, params={"query": 'rate({namespace="monitoring"}[1m])'}).status_code == 400
    assert upstream.requests == []
    query = '{namespace="monitoring"} |= "oom"'
    assert client.get(QUERY_INSTANT, params={"query": query, "time": "1700000000"}).status_code == 200
    params = _forwarded_params(upstream)
    assert params["query"] == [query]
    assert params["time"] == ["1700000000"]


def test_missing_query_param_rejected(client: TestClient, upstream: Upstream) -> None:
    for path in (QUERY_RANGE, QUERY_INSTANT):
        assert client.get(path).status_code == 400
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["/loki/api/v1/series", "/loki/api/v1/labels", "/loki/api/v1/tail", "/", "/metrics"])
def test_other_endpoints_404(client: TestClient, upstream: Upstream, path: str) -> None:
    assert client.get(path).status_code == 404
    assert upstream.requests == []


def test_non_get_rejected(client: TestClient, upstream: Upstream) -> None:
    query = {"query": '{namespace="monitoring"}'}
    assert client.post(QUERY_RANGE, params=query).status_code == 404
    assert client.post(QUERY_INSTANT, params=query).status_code == 404
    assert client.delete("/loki/api/v1/delete", params=query).status_code == 404
    assert upstream.requests == []


@pytest.mark.parametrize("limit", ["5001", "999999", "0", "-1", "abc", "10.5"])
def test_bad_limit_rejected(client: TestClient, upstream: Upstream, limit: str) -> None:
    resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}', "limit": limit})
    assert resp.status_code == 400
    assert upstream.requests == []


def test_max_limit_forwarded(client: TestClient, upstream: Upstream) -> None:
    resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}', "limit": str(MAX_LIMIT)})
    assert resp.status_code == 200
    assert _forwarded_params(upstream)["limit"] == [str(MAX_LIMIT)]


def test_passthrough_params_forwarded_and_unknown_dropped(client: TestClient, upstream: Upstream) -> None:
    resp = client.get(
        QUERY_RANGE,
        params={
            "query": '{namespace="monitoring"}',
            "start": "1700000000000000000",
            "end": "1700000600000000000",
            "direction": "backward",
            "step": "30s",
            "evil": "1",
        },
    )
    assert resp.status_code == 200
    params = _forwarded_params(upstream)
    assert params["start"] == ["1700000000000000000"]
    assert params["end"] == ["1700000600000000000"]
    assert params["direction"] == ["backward"]
    assert params["step"] == ["30s"]
    assert "evil" not in params


def test_duplicate_query_params_rejected(client: TestClient, upstream: Upstream) -> None:
    # A second `query` occurrence must not be smuggled past validation of the
    # first (or vice versa) — duplicates are rejected outright.
    for second in ('{namespace="matrix"}', '{namespace="monitoring"}'):
        resp = client.get(QUERY_RANGE, params=[("query", '{namespace="monitoring"}'), ("query", second)])
        assert resp.status_code == 400
    assert upstream.requests == []


def test_upstream_status_and_body_passed_through(upstream: Upstream) -> None:
    upstream.status_code = 500
    upstream.content = b'{"status":"error","message":"boom"}'
    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}'})
    assert resp.status_code == 500
    assert resp.content == upstream.content


def _client_whose_upstream_raises(exc: Exception) -> TestClient:
    def raise_it(request: httpx.Request) -> httpx.Response:
        raise exc

    return TestClient(create_app(SETTINGS, transport=httpx.MockTransport(raise_it)))


def test_upstream_timeout_is_504_not_500() -> None:
    # Regression: httpx.TimeoutException propagated out of the handler, so a slow
    # Loki query surfaced as a bare 500 with no body — indistinguishable from the
    # proxy itself crashing. Observed live 2026-07-31 on {namespace="monitoring"}.
    with _client_whose_upstream_raises(httpx.ReadTimeout("timed out")) as client:
        resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}'})
    assert resp.status_code == 504
    assert str(UPSTREAM_TIMEOUT_S) in resp.json()["detail"]


def test_upstream_unreachable_is_502() -> None:
    with _client_whose_upstream_raises(httpx.ConnectError("no route")) as client:
        resp = client.get(QUERY_RANGE, params={"query": '{namespace="monitoring"}'})
    assert resp.status_code == 502
    assert "no route" in resp.json()["detail"]


def test_upstream_timeout_takes_precedence_over_generic_request_error() -> None:
    # TimeoutException subclasses RequestError, so ordering the except clauses the
    # other way around would silently answer 502 for every timeout.
    assert issubclass(httpx.TimeoutException, httpx.RequestError)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAMESPACE_ALLOWLIST", " flux-system, monitoring ,kube-system ")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.delenv("UPSTREAM_URL", raising=False)
    monkeypatch.delenv("KUBERNETES_CA_FILE", raising=False)
    settings = Settings.from_env()
    assert settings.namespace_allowlist == ALLOWLIST
    assert settings.upstream_url == "http://loki-gateway.loki.svc:80"
    assert settings.kube_api_url == "https://10.96.0.1:443"
    assert settings.kube_ca_file == Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    monkeypatch.setenv("KUBERNETES_CA_FILE", "/var/run/kube-root-ca/ca.crt")
    assert Settings.from_env().kube_ca_file == Path("/var/run/kube-root-ca/ca.crt")

    monkeypatch.setenv("UPSTREAM_URL", "http://elsewhere:3100")
    assert Settings.from_env().upstream_url == "http://elsewhere:3100"

    monkeypatch.setenv("NAMESPACE_ALLOWLIST", " , ")
    with pytest.raises(ValueError, match="NAMESPACE_ALLOWLIST"):
        Settings.from_env()

    monkeypatch.delenv("NAMESPACE_ALLOWLIST")
    with pytest.raises(KeyError):
        Settings.from_env()


if __name__ == "__main__":
    pytest_bazel.main()
