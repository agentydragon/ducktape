"""Dedicated system test operator bootstrap through Dex's local password form; no client-side
OIDC/session implementation."""

import base64
import binascii
import json
import os
import re
import subprocess
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, SecretStr

from agentplane.app.oidc import INSECURE_COOKIE, SECURE_COOKIE

# The k3s fixture's own apiserver (`agentplane/system_tests/k3s`), which holds only its test operator.
K3S_FIXTURE_API = "https://127.0.0.1:6443"
SECRET_PATH_ENV = "AGENTPLANE_OPERATOR_SECRET_PATH"
_DEX_AUTHORIZATION_PATH = "/dex/auth"
_DEX_LOCAL_FORM_PATH = re.compile(r"/dex/auth/local(?:/login)?/?")
_DEX_LOCAL_LOGIN_PATH = re.compile(r"/dex/auth/local/login/?")
_DEX_OAUTH_PATH = re.compile(r"/dex/(auth|auth/local|auth/local/login|approval|callback)/?")


def _transport_allowed(url: httpx.URL) -> bool:
    """HTTPS, or HTTP to loopback/cluster Service names used by the disposable k3s fixture."""
    return url.scheme == "https" or (
        url.scheme == "http" and (url.host in {"localhost", "127.0.0.1", "::1"} or url.host.endswith(".svc"))
    )


class LoginBlockedError(Exception):
    """Only non-sensitive prerequisite failures cross this boundary; never response contents."""


def _is_action_not_found(payload: object) -> bool:
    """The Go facade uses google.rpc.Code; the Python BFF uses FastAPI's detail envelope."""
    if not isinstance(payload, dict):
        return False
    if payload.get("code") == 5:
        return True
    detail = payload.get("detail")
    return isinstance(detail, dict) and detail.get("code") == "NOT_FOUND"


async def verify_action_federation(http: httpx.AsyncClient) -> None:
    """An absent Action proves authenticated federation without listing anyone's requests."""
    __tracebackhide__ = True
    request_id = uuid4()
    try:
        response = await http.get(f"/upstream/actions/v1/action-requests/{request_id}")
        failure = response.json()
        if response.status_code == HTTPStatus.NOT_FOUND and _is_action_not_found(failure):
            return
    except httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError:
        # Malformed responses and transport exceptions may contain private auth material.
        pass
    raise LoginBlockedError(
        "BLOCKED: BFF Action federation preflight refused; verify dedicated operator target identity"
    ) from None


class OperatorCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    login: SecretStr
    username: SecretStr
    password: SecretStr
    issuer: SecretStr
    subject: SecretStr


def _kubectl(*args: str) -> bytes:
    __tracebackhide__ = True
    try:
        result = subprocess.run(
            ["kubectl", "--v=0", "--request-timeout=20s", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        raise LoginBlockedError("BLOCKED: kubectl/kubeconfig unavailable or the apiserver timed out") from None
    if result.returncode:
        raise LoginBlockedError("BLOCKED: named Secret GET unavailable; check kubeconfig and RBAC")
    return result.stdout


def read_operator_credentials() -> OperatorCredentials:
    __tracebackhide__ = True
    # Project only the active server, never raw kubeconfig credentials. Reject any other cluster.
    server = _kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    if server.strip() != K3S_FIXTURE_API.encode():
        raise LoginBlockedError("BLOCKED: current kubeconfig must use the k3s fixture's apiserver")
    secret_path = os.environ[SECRET_PATH_ENV]
    if (
        re.fullmatch(
            r"/api/v1/namespaces/[a-z0-9]([-a-z0-9]*[a-z0-9])?/secrets/[a-z0-9]([-a-z0-9]*[a-z0-9])?", secret_path
        )
        is None
    ):
        raise LoginBlockedError("BLOCKED: operator Secret path must name one Kubernetes Secret")
    raw = _kubectl("get", f"--raw={secret_path}")
    try:
        data = json.loads(raw)["data"]
        values = {
            key: base64.b64decode(data[key], validate=True).decode()
            for key in ("login", "username", "password", "issuer", "subject")
        }
        if not all(values.values()):
            raise ValueError
        return OperatorCredentials(**{key: SecretStr(value) for key, value in values.items()})
    except KeyError, TypeError, ValueError, binascii.Error:
        raise LoginBlockedError(
            "BLOCKED: operator Secret requires nonempty base64 login/username/password/issuer/subject"
        ) from None


def _origin(url: httpx.URL) -> httpx.URL:
    return url.copy_with(path="/", query=None, fragment=None)


def app_origin(base_url: str) -> httpx.URL:
    __tracebackhide__ = True
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL:
        raise LoginBlockedError("BLOCKED: invalid BFF app origin") from None
    if not _transport_allowed(url) or url.userinfo or url.query or url.fragment or url.path != "/":
        raise LoginBlockedError("BLOCKED: BFF login requires an HTTPS app origin without credentials or path")
    return _origin(url)


def _destination(source: httpx.URL, location: str, app: httpx.URL, dex: httpx.URL) -> httpx.URL:
    __tracebackhide__ = True
    url = source.join(location)
    if not _transport_allowed(url) or url.userinfo or url.fragment:
        raise LoginBlockedError("BLOCKED: unsafe OIDC redirect")
    if (_origin(url) == app and url.path == "/auth/callback") or (
        _origin(url) == dex and _DEX_OAUTH_PATH.fullmatch(url.path)
    ):
        return url
    raise LoginBlockedError("BLOCKED: OIDC redirect outside the app callback or Dex authorization endpoints")


@dataclass
class DexPasswordLogin:
    """One guarded submission of Dex's local password form.

    Dex's local connector puts its short-lived authorization request ID in the form action query.
    Retaining that action unchanged is essential; credentials are added only to a same-origin
    HTTPS local-login POST.  Both the app bootstrap and the separate MCP authorization use this
    narrow provider adapter.
    """

    dex: httpx.URL
    password_sent: bool = False

    async def submit(
        self, browser: httpx.AsyncClient, response: httpx.Response, credentials: OperatorCredentials
    ) -> httpx.Response:
        __tracebackhide__ = True
        if self.password_sent:
            raise LoginBlockedError("BLOCKED: Dex rejected the operator password; not retrying credentials")
        if _origin(response.url) != self.dex or _DEX_LOCAL_FORM_PATH.fullmatch(response.url.path) is None:
            raise LoginBlockedError("BLOCKED: Dex password form was not served by the configured local-login endpoint")
        form = BeautifulSoup(response.text, "html.parser").find("form")
        if form is None or not form.get("action"):
            raise LoginBlockedError("BLOCKED: Dex login form was not found")
        target = response.url.join(str(form["action"]))
        if (
            not _transport_allowed(target)
            or target.userinfo
            or target.fragment
            or _origin(target) != self.dex
            or _DEX_LOCAL_LOGIN_PATH.fullmatch(target.path) is None
        ):
            raise LoginBlockedError("BLOCKED: Dex login form submitted credentials outside its local-login endpoint")
        payload = {
            str(input_tag["name"]): str(input_tag.get("value", ""))
            for input_tag in form.find_all("input")
            if input_tag.get("name")
        }
        payload.update(login=credentials.login.get_secret_value(), password=credentials.password.get_secret_value())
        self.password_sent = True
        return await browser.post(
            target, data=payload, headers={"Origin": str(self.dex).rstrip("/"), "Referer": str(response.url)}
        )


def _required_query_value(url: httpx.URL, name: str, *, stage: str) -> str:
    values = parse_qs(url.query.decode(), keep_blank_values=True).get(name, [])
    if len(values) != 1 or not values[0]:
        raise LoginBlockedError(f"BLOCKED: {stage} omitted an unambiguous OAuth {name}")
    return values[0]


def _dex_browser_cookies(browser: httpx.AsyncClient, dex: httpx.URL) -> None:
    if any(cookie.domain is None or cookie.domain.lstrip(".") != dex.host for cookie in browser.cookies.jar):
        raise LoginBlockedError("BLOCKED: Dex authorization returned a cookie for another origin")


async def follow_dex_authorization(
    authorization_url: str,
    browser: httpx.AsyncClient,
    credentials: OperatorCredentials,
    *,
    callback_app: httpx.URL,
    callback_path: str,
) -> tuple[str, str]:
    """Authorize a separate OAuth client through a fresh Dex local-password browser.

    The browser never follows the application callback: it returns its state/code to the
    application client that initiated the linkage.  That keeps app-session cookies out of Dex and
    leaves callback validation and the token exchange at the existing BFF boundary.
    """

    __tracebackhide__ = True
    try:
        authorization = httpx.URL(authorization_url)
        issuer = httpx.URL(credentials.issuer.get_secret_value())
    except httpx.InvalidURL:
        raise LoginBlockedError("BLOCKED: Dex authorization URL or operator issuer is invalid") from None
    if not _transport_allowed(issuer) or issuer.userinfo or issuer.query or issuer.fragment:
        raise LoginBlockedError("BLOCKED: operator Secret has an invalid HTTPS issuer")
    dex = _origin(issuer)
    app = app_origin(str(callback_app))
    if (
        not _transport_allowed(authorization)
        or authorization.userinfo
        or authorization.fragment
        or _origin(authorization) != dex
        or authorization.path != _DEX_AUTHORIZATION_PATH
    ):
        raise LoginBlockedError("BLOCKED: MCP authorization did not target the configured Dex authorization endpoint")
    if callback_path != "/mcp-linkage/callback":
        raise LoginBlockedError("BLOCKED: MCP authorization callback path is not allowed")
    if browser.cookies or "Authorization" in browser.headers:
        raise LoginBlockedError(
            "BLOCKED: Dex linkage requires a fresh browser without app session or bearer credentials"
        )
    original_state = _required_query_value(authorization, "state", stage="MCP authorization")
    # Do not rebuild the URL: the BFF-owned state and PKCE challenge enter Dex byte-for-byte here.
    _required_query_value(authorization, "code_challenge", stage="MCP authorization")
    password_login = DexPasswordLogin(dex)

    response = await browser.get(authorization)
    for _ in range(12):
        _dex_browser_cookies(browser, dex)
        if response.is_redirect:
            if response.request.method == "POST" and response.status_code in (307, 308):
                raise LoginBlockedError("BLOCKED: Dex requested replay of a credential POST")
            location = response.headers.get("location")
            if not location:
                raise LoginBlockedError("BLOCKED: Dex returned a redirect without a location")
            target = response.url.join(location)
            if not _transport_allowed(target) or target.userinfo or target.fragment:
                raise LoginBlockedError("BLOCKED: Dex returned an unsafe MCP authorization redirect")
            if _origin(target) == dex and _DEX_OAUTH_PATH.fullmatch(target.path):
                response = await browser.get(target)
                continue
            if _origin(target) == app and target.path == callback_path:
                state = _required_query_value(target, "state", stage="Dex callback")
                if state != original_state:
                    raise LoginBlockedError("BLOCKED: Dex callback changed the BFF OAuth state")
                return state, _required_query_value(target, "code", stage="Dex callback")
            raise LoginBlockedError("BLOCKED: Dex returned a redirect outside the MCP callback")
        if response.status_code != 200:
            raise LoginBlockedError(f"BLOCKED: Dex authorization returned HTTP {response.status_code}")
        response = await password_login.submit(browser, response, credentials)
    raise LoginBlockedError("BLOCKED: Dex authorization exceeded its redirect bound")


async def login_operator(http: httpx.AsyncClient, credentials: OperatorCredentials) -> None:
    """Follow app-created state/PKCE/nonce unchanged; callback alone issues the logged-in cookie.

    Caller must suppress HTTP logging and local-variable dumps for this sensitive exchange.
    No bearer headers or prepopulated cookies belong on this fresh client.
    """
    __tracebackhide__ = True
    app = app_origin(str(http.base_url))
    issuer = httpx.URL(credentials.issuer.get_secret_value())
    if not _transport_allowed(issuer) or issuer.userinfo or issuer.query or issuer.fragment:
        raise LoginBlockedError("BLOCKED: operator Secret has an invalid HTTPS issuer")
    dex = _origin(issuer)
    if app.host == dex.host or app.host.endswith(f".{dex.host}") or dex.host.endswith(f".{app.host}"):
        raise LoginBlockedError("BLOCKED: app and Dex require separate, non-overlapping cookie hosts")
    if http.cookies or "Authorization" in http.headers:
        raise LoginBlockedError("BLOCKED: OIDC login requires a fresh cookie jar without bearer authentication")
    response = await http.get("/auth/login")
    password_login = DexPasswordLogin(dex)
    for _ in range(20):
        # Refuse broad-domain cookies before another request can send them to the other origin.
        if any(cookie.domain_specified or cookie.domain not in (app.host, dex.host) for cookie in http.cookies.jar):
            raise LoginBlockedError("BLOCKED: login returned a cookie spanning untrusted origins")
        if _origin(response.url) == app and response.url.path == "/auth/callback":
            if response.status_code != 303 or response.headers.get("location") != "/":
                raise LoginBlockedError("BLOCKED: app OIDC callback rejected authorization")
            # The app names its session cookie by its own origin's scheme (`OIDCSettings.cookie_name`).
            cookie_name = SECURE_COOKIE if app.scheme == "https" else INSECURE_COOKIE
            cookie = http.cookies.get(cookie_name, domain=app.host, path="/")
            if not cookie:
                raise LoginBlockedError("BLOCKED: app callback did not issue an operator session cookie")
            me = await http.get("/auth/me")
            if me.status_code != 200 or me.json() != {"username": credentials.username.get_secret_value()}:
                raise LoginBlockedError("BLOCKED: app OIDC session does not identify the dedicated operator")
            return
        if response.is_redirect:
            if response.request.method == "POST" and response.status_code in (307, 308):
                raise LoginBlockedError("BLOCKED: Dex requested replay of a credential POST")
            response = await http.get(_destination(response.url, response.headers["location"], app, dex))
            continue
        if response.status_code != 200:
            stage = "app login" if _origin(response.url) == app else "Dex authorization"
            raise LoginBlockedError(f"BLOCKED: {stage} returned HTTP {response.status_code}")
        response = await password_login.submit(http, response, credentials)
    raise LoginBlockedError("BLOCKED: Dex login exceeded its redirect bound")
