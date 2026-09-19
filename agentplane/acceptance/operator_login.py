"""Dedicated acceptance operator bootstrap; no client-side OIDC/session implementation.

Authentik 2026.2.1's FlowExecutor passes the UI query as `query`, and solves
identification/password challenges with the component token and Django CSRF cookie.
Only that non-interactive path is supported; other stages require operator action.
"""

import base64
import binascii
import json
import os
import re
import subprocess
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, SecretStr

from agentplane.app.action_federation import UpstreamFailure
from agentplane.app.oidc import SECURE_COOKIE

KUBE_PROXY = "https://haku-kubeapi.allegedly.works"
DEFAULT_SECRET_PATH = "/api/v1/namespaces/public-coder-agent/secrets/agentplane-testing-acceptance-operator"
SECRET_PATH = DEFAULT_SECRET_PATH
_DEX_AUTHORIZATION_PATH = "/dex/auth"
_DEX_LOCAL_FORM_PATH = re.compile(r"/dex/auth/local(?:/login)?/?")
_DEX_LOCAL_LOGIN_PATH = re.compile(r"/dex/auth/local/login/?")
_DEX_OAUTH_PATH = re.compile(r"/dex/(auth|auth/local|auth/local/login|approval|callback)/?")


class LoginBlockedError(Exception):
    """Only non-sensitive prerequisite failures cross this boundary; never response contents."""


async def verify_action_federation(http: httpx.AsyncClient) -> None:
    """An absent Action proves authenticated federation without listing anyone's requests."""
    __tracebackhide__ = True
    request_id = uuid4()
    try:
        response = await http.get(f"/actions/{request_id}")
        failure = UpstreamFailure.model_validate(response.json()["detail"])
        upstream = httpx.URL(failure.url)
        if (
            response.status_code == HTTPStatus.NOT_FOUND
            and failure.upstream_status == HTTPStatus.NOT_FOUND
            and failure.method == "GET"
            and failure.error_type == "HTTPStatusError"
            and upstream.scheme in {"http", "https"}
            and upstream.host
            and not (upstream.userinfo or upstream.query or upstream.fragment)
            and upstream.path == f"/v1/operator/action-requests/{request_id}"
        ):
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
        raise LoginBlockedError("BLOCKED: kubectl/kubeconfig unavailable or Kubernetes proxy timed out") from None
    if result.returncode:
        raise LoginBlockedError(
            "BLOCKED: proxied named Secret GET unavailable; check kubeconfig, proxy, RBAC and reflection"
        )
    return result.stdout


def read_operator_credentials() -> OperatorCredentials:
    __tracebackhide__ = True
    # Project only the active server, never raw kubeconfig credentials. Reject direct cluster access.
    server = _kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    if server.strip() != KUBE_PROXY.encode():
        raise LoginBlockedError("BLOCKED: current kubeconfig must use the Haku Console Kubernetes proxy")
    secret_path = os.environ.get("AGENTPLANE_OPERATOR_SECRET_PATH", DEFAULT_SECRET_PATH)
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
            "BLOCKED: reflected operator Secret requires nonempty base64 login/username/password/issuer/subject"
        ) from None


def _origin(url: httpx.URL) -> httpx.URL:
    return url.copy_with(path="/", query=None, fragment=None)


def app_origin(base_url: str) -> httpx.URL:
    __tracebackhide__ = True
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL:
        raise LoginBlockedError("BLOCKED: invalid BFF app origin") from None
    if url.scheme != "https" or url.userinfo or url.query or url.fragment or url.path != "/":
        raise LoginBlockedError("BLOCKED: BFF acceptance requires an HTTPS app origin without credentials or path")
    return _origin(url)


def _destination(
    source: httpx.URL, location: str, app: httpx.URL, idp: httpx.URL, provider: Literal["authentik", "dex"]
) -> httpx.URL:
    __tracebackhide__ = True
    url = source.join(location)
    if url.scheme != "https" or url.userinfo or url.fragment:
        raise LoginBlockedError("BLOCKED: unsafe OIDC redirect")
    if _origin(url) == app and url.path == "/auth/callback":
        return url
    if _origin(url) == idp and (
        url.path == "/application/o/authorize/"
        or re.fullmatch(r"/if/flow/[a-zA-Z0-9_-]+/", url.path)
        # stage_ok returns a same-executor 302, followed as GET by Authentik's fetch client.
        or (
            _origin(source) == idp
            and url.path == source.path
            and re.fullmatch(r"/api/v3/flows/executor/[a-zA-Z0-9_-]+/", url.path)
        )
        or (
            provider == "dex" and re.fullmatch(r"/dex/(auth|auth/local|auth/local/login|approval|callback)/?", url.path)
        )
    ):
        return url
    raise LoginBlockedError("BLOCKED: OIDC redirect outside the app callback or Authentik authorization/flow endpoints")


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
            target.scheme != "https"
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
    if issuer.scheme != "https" or issuer.userinfo or issuer.query or issuer.fragment:
        raise LoginBlockedError("BLOCKED: operator Secret has an invalid HTTPS issuer")
    dex = _origin(issuer)
    app = app_origin(str(callback_app))
    if (
        authorization.scheme != "https"
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
            if target.scheme != "https" or target.userinfo or target.fragment:
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


async def login_operator(
    http: httpx.AsyncClient, credentials: OperatorCredentials, *, provider: Literal["authentik", "dex"] = "authentik"
) -> None:
    """Follow app-created state/PKCE/nonce unchanged; callback alone issues the logged-in cookie.

    Caller must suppress HTTP logging and local-variable dumps for this sensitive exchange.
    No bearer headers or prepopulated cookies belong on this fresh client.
    """
    __tracebackhide__ = True
    app = app_origin(str(http.base_url))
    issuer = httpx.URL(credentials.issuer.get_secret_value())
    if issuer.scheme != "https" or issuer.userinfo or issuer.query or issuer.fragment:
        raise LoginBlockedError("BLOCKED: operator Secret has an invalid HTTPS issuer")
    idp = _origin(issuer)
    if app.host == idp.host or app.host.endswith(f".{idp.host}") or idp.host.endswith(f".{app.host}"):
        raise LoginBlockedError("BLOCKED: app and Authentik require separate, non-overlapping cookie hosts")
    if http.cookies or "Authorization" in http.headers:
        raise LoginBlockedError("BLOCKED: OIDC login requires a fresh cookie jar without bearer authentication")
    response = await http.get("/auth/login")
    identified = False
    password_sent = False
    dex_password_login = DexPasswordLogin(idp)
    # Bounds include both HTTP redirects and FlowExecutor stages; never retry a rejected password.
    for _ in range(20):
        # Refuse broad-domain cookies before another request can send them to the other origin.
        if any(cookie.domain_specified or cookie.domain not in (app.host, idp.host) for cookie in http.cookies.jar):
            raise LoginBlockedError("BLOCKED: login returned a cookie spanning untrusted origins")
        if _origin(response.url) == app and response.url.path == "/auth/callback":
            if response.status_code != 303 or response.headers.get("location") != "/":
                raise LoginBlockedError("BLOCKED: app OIDC callback rejected authorization")
            cookie = http.cookies.get(SECURE_COOKIE, domain=app.host, path="/")
            if not cookie:
                raise LoginBlockedError("BLOCKED: app callback did not issue an operator session cookie")
            me = await http.get("/auth/me")
            if me.status_code != 200 or me.json() != {"username": credentials.username.get_secret_value()}:
                raise LoginBlockedError("BLOCKED: app OIDC session does not identify the dedicated operator")
            return
        if response.is_redirect:
            if response.request.method == "POST" and response.status_code in (307, 308):
                raise LoginBlockedError("BLOCKED: Authentik requested replay of a credential POST")
            target = _destination(response.url, response.headers["location"], app, idp, provider)
            response = await http.get(target)
            continue
        if response.status_code != 200:
            stage = "app login" if _origin(response.url) == app else f"{provider} authorization"
            raise LoginBlockedError(f"BLOCKED: {stage} returned HTTP {response.status_code}")
        if provider == "dex" and _DEX_LOCAL_FORM_PATH.fullmatch(response.url.path):
            response = await dex_password_login.submit(http, response, credentials)
            continue
        if re.fullmatch(r"/if/flow/[a-zA-Z0-9_-]+/", response.url.path):
            # GET the UI first to obtain Authentik's normal session/CSRF cookies, then do what its UI does.
            flow_page = response.url
            slug = flow_page.path.split("/")[-2]
            executor = idp.join(f"/api/v3/flows/executor/{slug}/").copy_merge_params(
                {"query": flow_page.query.decode()}
            )
            response = await http.get(executor)
            continue
        challenge = response.json()
        if not isinstance(challenge, dict) or challenge.get("response_errors"):
            raise LoginBlockedError("BLOCKED: Authentik challenge rejected the dedicated operator login")
        if challenge.get("captcha_stage"):
            raise LoginBlockedError("BLOCKED: Authentik requires an interactive CAPTCHA challenge")
        component = challenge.get("component")
        if component == "xak-flow-redirect":
            target = _destination(response.url, challenge["to"], app, idp, provider)
            response = await http.get(target)
            continue
        payload: dict[str, str] = {}
        if component == "ak-stage-identification" and not identified:
            payload = {"component": component, "uid_field": credentials.login.get_secret_value()}
            identified = True
            if challenge.get("password_fields"):
                payload["password"] = credentials.password.get_secret_value()
                password_sent = True
        elif component == "ak-stage-password" and identified and not password_sent:
            payload = {"component": component, "password": credentials.password.get_secret_value()}
            password_sent = True
        else:
            raise LoginBlockedError("BLOCKED: Authentik requires an unsupported interactive/MFA/consent challenge")
        # Authentik 2026.2's flow executor may not issue its CSRF cookie for an OIDC login
        # started through /application/o/authorize/. When it does issue one, echo it through
        # Authentik's current header; the app's state/nonce/PKCE checks remain mandatory either way.
        csrf = http.cookies.get("authentik_csrf", domain=idp.host, path="/")
        headers = {"Origin": str(idp).rstrip("/"), "Referer": str(flow_page)}
        if csrf:
            headers["X-Authentik-CSRF"] = csrf
        response = await http.post(executor, json=payload, headers=headers)
    raise LoginBlockedError("BLOCKED: Authentik login exceeded its redirect/stage bound")
