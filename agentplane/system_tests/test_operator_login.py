"""Offline choreography/sanitization checks, not evidence of a deployed OIDC login."""

import base64
import json
import subprocess
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from pydantic import JsonValue, SecretStr

from agentplane.app.oidc import SECURE_COOKIE
from agentplane.system_tests.operator_login import (
    K3S_FIXTURE_API,
    SECRET_PATH_ENV,
    LoginBlockedError,
    OperatorCredentials,
    app_origin,
    follow_dex_authorization,
    login_operator,
    read_operator_credentials,
    verify_action_federation,
)

APP = "https://app.test.invalid"
DEX = "https://dex.test.invalid"
CREDENTIALS = OperatorCredentials(
    login=SecretStr("test-user@example.invalid"),
    username=SecretStr("test-user"),
    password=SecretStr("test-password"),
    issuer=SecretStr(f"{DEX}/dex"),
    subject=SecretStr("test-subject"),
)


def _missing_action_error(request: httpx.Request, *, python_bff: bool = False) -> dict[str, JsonValue]:
    UUID(request.url.path.removeprefix("/upstream/actions/v1/action-requests/"))
    if python_bff:
        return {"detail": {"code": "NOT_FOUND", "message": "unknown action request"}}
    return {"code": 5, "message": "not found", "details": []}


@pytest.mark.parametrize("python_bff", [False, True])
async def test_federation_preflight_accepts_only_the_correlated_upstream_not_found(python_bff: bool) -> None:
    probes: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        probes.append(request)
        assert request.method == "GET"
        assert request.url.host == "app.test.invalid"
        return httpx.Response(404, json=_missing_action_error(request, python_bff=python_bff))

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        await verify_action_federation(http)
        await verify_action_federation(http)
    assert len(probes) == 2
    assert probes[0].url != probes[1].url
    assert all(
        probe.url.path.startswith("/upstream/actions/v1/action-requests/") and probe.url.path.count("/") == 5
        for probe in probes
    )


@pytest.mark.parametrize(("field", "value"), [("code", 14), ("code", "5")])
async def test_federation_preflight_rejects_other_upstream_failures(field: str, value: JsonValue) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        error = _missing_action_error(request)
        error[field] = value
        return httpx.Response(404, json=error)

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError, match="BFF Action federation preflight refused"):
            await verify_action_federation(http)


@pytest.mark.parametrize("status", [200, 401, 403, 503])
async def test_federation_preflight_requires_the_bff_not_found_status(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=_missing_action_error(request))

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError, match="BFF Action federation preflight refused"):
            await verify_action_federation(http)


@pytest.mark.parametrize("failure", ["html", "legacy", "malformed", "transport", "private-body"])
async def test_federation_preflight_withholds_response_and_transport_details(
    failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "must-not-be-in-failure-output"

    def respond(request: httpx.Request) -> httpx.Response:
        match failure:
            case "html":
                return httpx.Response(404, text=marker)
            case "legacy":
                return httpx.Response(404, json={"detail": "Action Service rejected the request"})
            case "malformed":
                return httpx.Response(404, json={"code": marker})
            case "transport":
                raise httpx.ConnectError(marker, request=request)
            case "private-body":
                return httpx.Response(404, json={"code": 14, "message": marker, "details": []})
            case _:
                raise AssertionError(failure)

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError, match="BFF Action federation preflight refused") as caught:
            await verify_action_federation(http)
    assert marker not in str(caught.value)
    assert capsys.readouterr() == ("", "")


def _mcp_authorization() -> str:
    return (
        f"{DEX}/dex/auth?client_id=test-mcp-linkage&response_type=code&"
        f"redirect_uri={APP}/mcp-linkage/callback&state=bff-state&"
        "code_challenge=bff-pkce&code_challenge_method=S256"
    )


async def _follow_mcp_linkage(browser: httpx.AsyncClient) -> tuple[str, str]:
    return await follow_dex_authorization(
        _mcp_authorization(), browser, CREDENTIALS, callback_app=httpx.URL(APP), callback_path="/mcp-linkage/callback"
    )


SECRET_PATH = "/api/v1/namespaces/test-namespace/secrets/test-operator-login"


@pytest.fixture(autouse=True)
def secret_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_PATH_ENV, SECRET_PATH)


def test_secret_is_one_named_get_through_the_fixture_apiserver(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {name: base64.b64encode(value.get_secret_value().encode()).decode() for name, value in CREDENTIALS}
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=K3S_FIXTURE_API.encode()),
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"data": data}).encode()),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    assert read_operator_credentials() == CREDENTIALS
    assert run.call_args_list[0].args[0] == [
        "kubectl",
        "--v=0",
        "--request-timeout=20s",
        "config",
        "view",
        "--minify",
        "-o",
        "jsonpath={.clusters[0].cluster.server}",
    ]
    assert run.call_args_list[1].args[0] == ["kubectl", "--v=0", "--request-timeout=20s", "get", f"--raw={SECRET_PATH}"]
    for call in run.call_args_list:
        assert call.kwargs["stderr"] == subprocess.DEVNULL
        assert call.kwargs["stdout"] == subprocess.PIPE
        assert call.kwargs["timeout"] == 30


@pytest.mark.parametrize("failure", ["direct", "missing", "timeout", "denied", "malformed", "empty"])
def test_secret_failures_are_closed_and_do_not_echo_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    marker = "must-not-be-in-failure-output"
    results: list[object] = [subprocess.CompletedProcess([], 0, stdout=K3S_FIXTURE_API.encode())]
    if failure == "direct":
        results = [subprocess.CompletedProcess([], 0, stdout=b"https://other-cluster.example.test")]
    elif failure == "missing":
        results = [FileNotFoundError(marker)]
    elif failure == "timeout":
        results.append(subprocess.TimeoutExpired("kubectl", 30, output=marker))
    elif failure == "denied":
        results.append(subprocess.CompletedProcess([], 1, stdout=marker.encode(), stderr=marker.encode()))
    elif failure == "malformed":
        results.append(subprocess.CompletedProcess([], 0, stdout=marker.encode()))
    else:
        results.append(
            subprocess.CompletedProcess(
                [], 0, stdout=b'{"data":{"login":"","username":"","password":"","issuer":"","subject":""}}'
            )
        )
    run = Mock(side_effect=results)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(LoginBlockedError, match="BLOCKED") as caught:
        read_operator_credentials()
    assert marker not in str(caught.value)
    assert capsys.readouterr() == ("", "")
    assert run.call_count == len(results)


async def test_dex_login_uses_simple_local_form_and_preserves_app_oidc_flow() -> None:
    steps: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        steps.append(f"{request.method} {request.url.path}")
        if request.url.path == "/auth/login":
            return httpx.Response(
                302,
                headers={
                    "location": f"{DEX}/dex/auth?state=server-state&code_challenge=server-pkce",
                    "set-cookie": f"{SECURE_COOKIE}=pending; Path=/; Secure; HttpOnly",
                },
            )
        if request.url.path == "/dex/auth":
            assert dict(request.url.params) == {"state": "server-state", "code_challenge": "server-pkce"}
            return httpx.Response(
                302, headers={"location": f"{DEX}/dex/auth/local?state=server-state&code_challenge=server-pkce"}
            )
        if request.url.path == "/dex/auth/local" and request.method == "GET":
            assert dict(request.url.params) == {"state": "server-state", "code_challenge": "server-pkce"}
            return httpx.Response(302, headers={"location": "/dex/auth/local/login?state=dex-request&back="})
        if request.url.path == "/dex/auth/local/login" and request.method == "GET":
            return httpx.Response(
                200, text='<form method="post" action="/dex/auth/local/login?state=dex-request&amp;back="></form>'
            )
        if request.url.path == "/dex/auth/local/login" and request.method == "POST":
            assert dict(request.url.params) == {"state": "dex-request", "back": ""}
            assert request.content == b"login=test-user%40example.invalid&password=test-password"
            return httpx.Response(302, headers={"location": f"{APP}/auth/callback?state=server-state&code=***"})
        if request.url.path == "/auth/callback":
            return httpx.Response(
                303, headers={"location": "/", "set-cookie": f"{SECURE_COOKIE}=app-issued; Path=/; Secure; HttpOnly"}
            )
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"username": "test-user"})
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        await login_operator(http, CREDENTIALS)
    assert steps == [
        "GET /auth/login",
        "GET /dex/auth",
        "GET /dex/auth/local",
        "GET /dex/auth/local/login",
        "POST /dex/auth/local/login",
        "GET /auth/callback",
        "GET /auth/me",
    ]


async def test_dex_linkage_uses_a_fresh_local_form_and_preserves_bff_state_pkce() -> None:
    authorization = _mcp_authorization()
    steps: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        steps.append(f"{request.method} {request.url.path}")
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if request.url.path == "/dex/auth":
            assert str(request.url) == authorization
            return httpx.Response(
                302,
                headers={
                    "location": "/dex/auth/local?client_id=test-mcp-linkage&response_type=code&"
                    f"redirect_uri={APP}/mcp-linkage/callback&state=bff-state&"
                    "code_challenge=bff-pkce&code_challenge_method=S256"
                },
            )
        if request.url.path == "/dex/auth/local":
            assert dict(request.url.params) == {
                "client_id": "test-mcp-linkage",
                "response_type": "code",
                "redirect_uri": f"{APP}/mcp-linkage/callback",
                "state": "bff-state",
                "code_challenge": "bff-pkce",
                "code_challenge_method": "S256",
            }
            return httpx.Response(302, headers={"location": "/dex/auth/local/login?state=dex-request&back="})
        if request.url.path == "/dex/auth/local/login" and request.method == "GET":
            assert dict(request.url.params) == {"state": "dex-request", "back": ""}
            return httpx.Response(
                200, text='<form method="post" action="/dex/auth/local/login?state=dex-request&amp;back="></form>'
            )
        if request.url.path == "/dex/auth/local/login" and request.method == "POST":
            assert dict(request.url.params) == {"state": "dex-request", "back": ""}
            assert request.headers["origin"] == DEX
            assert request.headers["referer"] == f"{DEX}/dex/auth/local/login?state=dex-request&back="
            assert request.content == b"login=test-user%40example.invalid&password=test-password"
            return httpx.Response(
                303, headers={"location": f"{APP}/mcp-linkage/callback?state=bff-state&code=provider-code"}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as browser:
        assert await _follow_mcp_linkage(browser) == ("bff-state", "provider-code")
    assert steps == ["GET /dex/auth", "GET /dex/auth/local", "GET /dex/auth/local/login", "POST /dex/auth/local/login"]


async def test_dex_linkage_refuses_nonlocal_form_targets_without_sending_credentials() -> None:
    marker = "must-not-appear-in-error"
    posts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
        if request.url.path == "/dex/auth":
            return httpx.Response(302, headers={"location": "/dex/auth/local/login?state=dex-request"})
        if request.url.path == "/dex/auth/local/login":
            return httpx.Response(200, text=f'<form method="post" action="https://untrusted.invalid/{marker}"></form>')
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as browser:
        with pytest.raises(LoginBlockedError) as caught:
            await _follow_mcp_linkage(browser)
    assert marker not in str(caught.value)
    assert posts == 0


async def test_dex_linkage_refuses_credential_post_replay() -> None:
    posts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/dex/auth":
            return httpx.Response(302, headers={"location": "/dex/auth/local/login?state=dex-request"})
        if request.url.path == "/dex/auth/local/login" and request.method == "GET":
            return httpx.Response(200, text='<form method="post" action="?state=dex-request"></form>')
        if request.url.path == "/dex/auth/local/login" and request.method == "POST":
            posts += 1
            return httpx.Response(307, headers={"location": "/dex/auth/local/login?state=dex-request"})
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as browser:
        with pytest.raises(LoginBlockedError, match="replay"):
            await _follow_mcp_linkage(browser)
    assert posts == 1


async def test_dex_linkage_does_not_retry_a_rejected_password_form() -> None:
    marker = "must-not-appear-in-error"
    posts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/dex/auth":
            return httpx.Response(302, headers={"location": "/dex/auth/local/login?state=dex-request"})
        if request.url.path == "/dex/auth/local/login" and request.method == "GET":
            return httpx.Response(200, text='<form method="post" action="?state=dex-request"></form>')
        if request.url.path == "/dex/auth/local/login" and request.method == "POST":
            posts += 1
            return httpx.Response(200, text=f'<form method="post" action="?state=dex-request"><p>{marker}</p></form>')
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as browser:
        with pytest.raises(LoginBlockedError) as caught:
            await _follow_mcp_linkage(browser)
    assert marker not in str(caught.value)
    assert posts == 1


async def test_dex_linkage_refuses_a_browser_with_an_app_session_cookie() -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as browser:
        browser.cookies.set(SECURE_COOKIE, "app-issued", domain="app.test.invalid", path="/")
        with pytest.raises(LoginBlockedError, match="fresh browser"):
            await _follow_mcp_linkage(browser)
    assert requests == 0


@pytest.mark.parametrize("at_app", [True, False], ids=["app", "dex"])
@pytest.mark.parametrize("status", [401, 503])
async def test_login_failure_reports_boundary_without_response_details(at_app: bool, status: int) -> None:
    marker = "must-not-expose-response-details"

    def respond(request: httpx.Request) -> httpx.Response:
        if not at_app and request.url.path == "/auth/login":
            return httpx.Response(302, headers={"location": f"{DEX}/dex/auth?state={marker}"})
        return httpx.Response(status, text=marker)

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError) as caught:
            await login_operator(http, CREDENTIALS)
    stage = "app login" if at_app else "Dex authorization"
    assert str(caught.value) == f"BLOCKED: {stage} returned HTTP {status}"


@pytest.mark.parametrize(
    "failure", ["foreign", "http", "userinfo", "wrong_callback", "broad_cookie", "loop", "callback"]
)
async def test_login_refuses_unsafe_or_unsupported_flow(failure: str) -> None:
    posts = 0
    gets = 0
    marker = "do-not-echo-authorization-query"

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts, gets
        if request.method == "POST":
            posts += 1
        else:
            gets += 1
        if request.url.path == "/auth/login":
            target = {
                "foreign": f"https://untrusted.invalid/?code={marker}",
                "http": f"http://dex.test.invalid/dex/auth?code={marker}",
                "userinfo": f"https://user@dex.test.invalid/dex/auth?code={marker}",
                "wrong_callback": f"{APP}/other?code={marker}",
                "callback": f"{APP}/auth/callback?code={marker}",
            }.get(failure, f"{DEX}/dex/auth?state={marker}")
            headers = {"location": target}
            if failure == "broad_cookie":
                headers["set-cookie"] = "bad=private; Domain=test.invalid; Path=/; Secure"
            return httpx.Response(302, headers=headers)
        if request.url.path == "/auth/callback":
            return httpx.Response(401, text=marker)
        # `loop`: Dex redirects to itself until the bound.
        return httpx.Response(302, headers={"location": str(request.url)})

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError, match="BLOCKED") as caught:
            await login_operator(http, CREDENTIALS)
    assert marker not in str(caught.value)
    assert posts == 0
    assert gets <= 21


def test_origin_normalization() -> None:
    assert app_origin(APP) == app_origin(f"{APP}/")


def test_http_reaches_only_a_cluster_internal_name() -> None:
    assert app_origin("http://app.test-namespace.svc:8080") == httpx.URL("http://app.test-namespace.svc:8080/")
    with pytest.raises(LoginBlockedError, match="HTTPS app origin"):
        app_origin("http://app.test.invalid")


if __name__ == "__main__":
    pytest_bazel.main()
