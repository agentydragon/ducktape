"""Offline choreography/sanitization checks, not evidence of a deployed OIDC login."""

import base64
import json
import subprocess
from unittest.mock import Mock

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr

from x.agentplane.acceptance.operator_login import (
    KUBE_PROXY,
    SECRET_PATH,
    LoginBlockedError,
    OperatorCredentials,
    app_origin,
    login_operator,
    read_operator_credentials,
)
from x.agentplane.app.oidc import SECURE_COOKIE

APP = "https://app.test.invalid"
IDP = "https://auth.test.invalid"
CREDENTIALS = OperatorCredentials(
    login=SecretStr("test-user"),
    username=SecretStr("test-user"),
    password=SecretStr("test-password"),
    issuer=SecretStr(f"{IDP}/application/o/actions/"),
    subject=SecretStr("test-subject"),
)


def test_secret_is_one_named_get_through_current_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {name: base64.b64encode(value.get_secret_value().encode()).decode() for name, value in CREDENTIALS}
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=KUBE_PROXY.encode()),
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
    results: list[object] = [subprocess.CompletedProcess([], 0, stdout=KUBE_PROXY.encode())]
    if failure == "direct":
        results = [subprocess.CompletedProcess([], 0, stdout=b"https://kubernetes.default.svc")]
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


@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("csrf_cookie", [False, True])
async def test_login_follows_bff_flow_csrf_and_callback(combined: bool, csrf_cookie: bool) -> None:
    # Test doubles deliberately stand in for the network only. Production never constructs a cookie.
    flow = f"{IDP}/if/flow/default-authentication-flow/?next=%2Fapplication%2Fo%2Fauthorize%2F"
    callback = f"{APP}/auth/callback?state=server-state&code=provider-code"
    steps: list[str] = []
    identified = False
    authenticated = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal identified, authenticated
        steps.append(request.url.path)
        assert "authorization" not in request.headers
        if request.url.host == "auth.test.invalid":
            assert SECURE_COOKIE not in request.headers.get("cookie", "")
        else:
            assert "authentik_csrf" not in request.headers.get("cookie", "")
        match request.url.path:
            case "/auth/login":
                return httpx.Response(
                    302,
                    headers={
                        "location": f"{IDP}/application/o/authorize/?state=server-state&code_challenge=server-pkce",
                        "set-cookie": f"{SECURE_COOKIE}=pending; Path=/; Secure; HttpOnly",
                    },
                )
            case "/application/o/authorize/":
                assert request.url.params["state"] == "server-state"
                assert request.url.params["code_challenge"] == "server-pkce"
                return httpx.Response(302, headers={"location": callback if authenticated else flow})
            case "/if/flow/default-authentication-flow/":
                headers = {"set-cookie": "authentik_csrf=test-csrf; Path=/; Secure"} if csrf_cookie else {}
                return httpx.Response(200, text="Flow UI", headers=headers)
            case "/api/v3/flows/executor/default-authentication-flow/":
                assert request.url.params["query"] == httpx.URL(flow).query.decode()
                if request.method == "GET":
                    if authenticated:
                        return httpx.Response(
                            200,
                            json={
                                "component": "xak-flow-redirect",
                                "to": f"{IDP}/application/o/authorize/?state=server-state&code_challenge=server-pkce",
                            },
                        )
                    if identified:
                        return httpx.Response(200, json={"component": "ak-stage-password"})
                    return httpx.Response(
                        200, json={"component": "ak-stage-identification", "password_fields": combined}
                    )
                assert request.headers["origin"] == IDP
                assert request.headers["referer"] == flow
                if csrf_cookie:
                    assert request.headers["x-authentik-csrf"] == "test-csrf"
                else:
                    assert "x-authentik-csrf" not in request.headers
                payload = json.loads(request.content)
                if payload["component"] == "ak-stage-identification":
                    expected = {"component": "ak-stage-identification", "uid_field": "test-user"}
                    if combined:
                        expected["password"] = "test-password"
                    assert payload == expected
                    identified = True
                    authenticated = combined
                else:
                    assert payload == {"component": "ak-stage-password", "password": "test-password"}
                    authenticated = True
                return httpx.Response(302, headers={"location": str(request.url)})
            case "/auth/callback":
                assert str(request.url) == callback
                assert request.headers["cookie"] == f"{SECURE_COOKIE}=pending"
                return httpx.Response(
                    303,
                    headers={"location": "/", "set-cookie": f"{SECURE_COOKIE}=app-issued; Path=/; Secure; HttpOnly"},
                )
            case "/auth/me":
                assert request.headers["cookie"] == f"{SECURE_COOKIE}=app-issued"
                return httpx.Response(200, json={"username": "test-user"})
        raise AssertionError("Unexpected request")

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        await login_operator(http, CREDENTIALS)
    assert steps[-2:] == ["/auth/callback", "/auth/me"]
    assert len(steps) == (9 if combined else 11)


async def test_dex_login_uses_simple_local_form_and_preserves_app_oidc_flow() -> None:
    dex = "https://dex.test.invalid"
    credentials = OperatorCredentials(
        login=SecretStr("test-user@example.invalid"),
        username=SecretStr("test-user"),
        password=SecretStr("test-password"),
        issuer=SecretStr(f"{dex}/dex"),
        subject=SecretStr("test-subject"),
    )
    steps: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        steps.append(f"{request.method} {request.url.path}")
        if request.url.path == "/auth/login":
            return httpx.Response(
                302,
                headers={
                    "location": f"{dex}/dex/auth?state=server-state&code_challenge=server-pkce",
                    "set-cookie": f"{SECURE_COOKIE}=pending; Path=/; Secure; HttpOnly",
                },
            )
        if request.url.path == "/dex/auth":
            return httpx.Response(302, headers={"location": f"{dex}/dex/auth/local?req=test-request"})
        if request.url.path == "/dex/auth/local" and request.method == "GET":
            return httpx.Response(
                200,
                text='<form method="post" action="/dex/auth/local/login"><input value="test-request" name="req"></form>',
            )
        if request.url.path == "/dex/auth/local/login" and request.method == "POST":
            assert request.content == b"req=test-request&login=test-user%40example.invalid&password=test-password"
            return httpx.Response(302, headers={"location": f"{APP}/auth/callback?state=server-state&code=***"})
        if request.url.path == "/auth/callback":
            return httpx.Response(
                303, headers={"location": "/", "set-cookie": f"{SECURE_COOKIE}=app-issued; Path=/; Secure; HttpOnly"}
            )
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"username": "test-user"})
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        await login_operator(http, credentials, provider="dex")
    assert steps == [
        "GET /auth/login",
        "GET /dex/auth",
        "GET /dex/auth/local",
        "POST /dex/auth/local/login",
        "GET /auth/callback",
        "GET /auth/me",
    ]


@pytest.mark.parametrize("at_app", [True, False], ids=["app", "dex"])
@pytest.mark.parametrize("status", [401, 503])
async def test_login_failure_reports_boundary_without_response_details(at_app: bool, status: int) -> None:
    marker = "must-not-expose-response-details"

    def respond(request: httpx.Request) -> httpx.Response:
        if not at_app and request.url.path == "/auth/login":
            return httpx.Response(302, headers={"location": f"{IDP}/dex/auth?state={marker}"})
        return httpx.Response(status, text=marker)

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError) as caught:
            await login_operator(http, CREDENTIALS, provider="dex")
    stage = "app login" if at_app else "dex authorization"
    assert str(caught.value) == f"BLOCKED: {stage} returned HTTP {status}"


@pytest.mark.parametrize(
    "failure",
    ["foreign", "http", "userinfo", "wrong_callback", "broad_cookie", "mfa", "rejected", "loop", "callback", "captcha"],
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
                "http": f"http://auth.test.invalid/if/flow/login/?code={marker}",
                "userinfo": f"https://user@auth.test.invalid/if/flow/login/?code={marker}",
                "wrong_callback": f"{APP}/other?code={marker}",
                "callback": f"{APP}/auth/callback?code={marker}",
            }.get(failure, f"{IDP}/if/flow/login/?next={marker}")
            headers = {"location": target}
            if failure == "broad_cookie":
                headers["set-cookie"] = "bad=private; Domain=test.invalid; Path=/; Secure"
            return httpx.Response(302, headers=headers)
        if request.url.path == "/auth/callback":
            return httpx.Response(401, text=marker)
        if request.url.path == "/if/flow/login/":
            if failure == "loop":
                return httpx.Response(302, headers={"location": str(request.url)})
            return httpx.Response(200)
        if failure == "rejected":
            return httpx.Response(200, json={"response_errors": {"password": [marker]}})
        if failure == "captcha":
            return httpx.Response(200, json={"component": "ak-stage-identification", "captcha_stage": {"key": marker}})
        return httpx.Response(200, json={"component": "ak-stage-authenticator-validate", "private": marker})

    async with httpx.AsyncClient(base_url=APP, transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LoginBlockedError, match="BLOCKED") as caught:
            await login_operator(http, CREDENTIALS)
    assert marker not in str(caught.value)
    assert posts == 0
    assert gets <= 21


def test_origin_normalization() -> None:
    assert app_origin(APP) == app_origin(f"{APP}/")


if __name__ == "__main__":
    pytest_bazel.main()
