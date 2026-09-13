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
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, SecretStr

from x.agentplane.app.oidc import SECURE_COOKIE

KUBE_PROXY = "https://haku-kubeapi.allegedly.works"
DEFAULT_SECRET_PATH = "/api/v1/namespaces/public-coder-agent/secrets/agentplane-testing-acceptance-operator"
SECRET_PATH = DEFAULT_SECRET_PATH


class LoginBlockedError(Exception):
    """Only constant, non-sensitive prerequisite failures cross this boundary."""


class OperatorCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

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
    except (OSError, subprocess.TimeoutExpired):
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
            for key in ("username", "password", "issuer", "subject")
        }
        if not all(values.values()):
            raise ValueError
        return OperatorCredentials(**{key: SecretStr(value) for key, value in values.items()})
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise LoginBlockedError(
            "BLOCKED: reflected operator Secret requires nonempty base64 username/password/issuer/subject"
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
            raise LoginBlockedError("BLOCKED: Authentik refused login; check user, flow and CSRF configuration")
        if provider == "dex" and re.fullmatch(r"/dex/auth/local(?:/login)?/?", response.url.path):
            # Dex's local connector is a normal HTML form. Keep the provider adapter deliberately
            # small: the app's authorization-code, state, nonce, PKCE, and callback checks remain
            # the contract under test; this only avoids reproducing Authentik's FlowExecutor.
            form = BeautifulSoup(response.text, "html.parser").find("form")
            if form is None or not form.get("action"):
                raise LoginBlockedError("BLOCKED: Dex login form was not found")
            target = _destination(response.url, str(form["action"]), app, idp, provider)
            dex_payload = {
                str(input_tag["name"]): str(input_tag.get("value", ""))
                for input_tag in form.find_all("input")
                if input_tag.get("name")
            }
            dex_payload.update(
                login=credentials.username.get_secret_value(), password=credentials.password.get_secret_value()
            )
            response = await http.post(
                target, data=dex_payload, headers={"Origin": str(idp).rstrip("/"), "Referer": str(response.url)}
            )
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
            payload = {"component": component, "uid_field": credentials.username.get_secret_value()}
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
