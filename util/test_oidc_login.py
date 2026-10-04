"""The login's durable rules: which claims and sessions are admitted, what the middleware refuses, and a real
login round trip against a mock IdP."""

import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI
from itsdangerous import TimestampSigner
from pydantic import SecretStr

from util.net import bind_free_port
from util.oidc_login import LoginConfig, LoginSession, install_login, valid_claims
from util.testing.asgi import serve_app
from util.testing.mock_oidc import RSAKeyPair, build_mock_oidc_app, generate_rsa_keypair

ISSUER = "https://idp.test/application/o/test/"
CLIENT_ID = "test-client"
OWNER = "test-owner-subject"
SIGNED_OUT_TEXT = "You are signed out of the test app."
BASE = LoginConfig(
    issuer=ISSUER,
    client_id=CLIENT_ID,
    client_secret=SecretStr("test-client-secret"),
    session_secret=SecretStr("test-session-secret"),
    session_seconds=28_800,
    public_base_url="http://app.test",
    cookie_name="test-login",
    public_paths={"/healthz", "/hook"},
    signed_out_text=SIGNED_OUT_TEXT,
    session_created_log=None,
    allowed_subject=OWNER,
)


def _claims(*, remove: tuple[str, ...] = (), **overrides: Any) -> dict[str, Any]:
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": time.time() + 600,
        "sub": "test-subject",
        "preferred_username": "test-user",
    } | overrides
    return {name: value for name, value in claims.items() if name not in remove}


ADMITTED_CLAIMS = {
    "one audience": _claims(),
    "audience list": _claims(aud=[CLIENT_ID]),
    "several audiences, azp names this client": _claims(aud=[CLIENT_ID, "other-client"], azp=CLIENT_ID),
    "integer expiry": _claims(exp=int(time.time()) + 600),
}

REFUSED_CLAIMS = {
    "not a mapping": ["sub"],
    "other issuer": _claims(iss="https://other.test/"),
    "no audience": _claims(remove=("aud",)),
    "audience of another client": _claims(aud="other-client"),
    "audience list of another client": _claims(aud=["other-client"]),
    "audience list with a non-string": _claims(aud=[CLIENT_ID, 7]),
    "several audiences, no azp": _claims(aud=[CLIENT_ID, "other-client"]),
    "several audiences, azp names another client": _claims(aud=[CLIENT_ID, "other-client"], azp="other-client"),
    "azp of another client": _claims(azp="other-client"),
    "no expiry": _claims(remove=("exp",)),
    "expired": _claims(exp=time.time() - 1),
    "not-a-number expiry": _claims(exp=float("nan")),
    "infinite expiry": _claims(exp=float("inf")),
    "boolean expiry": _claims(exp=True),
    "string expiry": _claims(exp="9999999999"),
    "no subject": _claims(remove=("sub",)),
    "empty subject": _claims(sub=""),
    "non-string subject": _claims(sub=7),
    "no username": _claims(remove=("preferred_username",)),
    "empty username": _claims(preferred_username=""),
}


@pytest.mark.parametrize("claims", ADMITTED_CLAIMS.values(), ids=ADMITTED_CLAIMS.keys())
def test_claims_for_this_issuer_and_client_are_admitted(claims: dict[str, Any]) -> None:
    assert valid_claims(claims, issuer=ISSUER, client_id=CLIENT_ID)


@pytest.mark.parametrize("claims", REFUSED_CLAIMS.values(), ids=REFUSED_CLAIMS.keys())
def test_claims_that_do_not_pin_issuer_client_expiry_and_user_are_refused(claims: Any) -> None:
    assert not valid_claims(claims, issuer=ISSUER, client_id=CLIENT_ID)


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/hook")
    async def hook() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/")
    async def page() -> dict[str, str]:
        return {"page": "home"}

    @app.post("/form")
    async def form() -> dict[str, str]:
        return {"form": "posted"}

    @app.get("/api/things")
    async def read_things() -> dict[str, str]:
        return {"things": "read"}

    @app.post("/api/things")
    async def write_things() -> dict[str, str]:
        return {"things": "written"}

    return app


def _browser(config: LoginConfig) -> httpx.AsyncClient:
    """A browser wired straight to the app, for what needs no identity provider: forged sessions."""
    app = _app()
    install_login(app, config)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_base_url)


def _session_payload(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "issuer": ISSUER,
        "subject": OWNER,
        "username": "test-user",
        "expires_at": time.time() + 600,
    }
    return LoginSession(**(fields | overrides)).model_dump()


def _cookie(config: LoginConfig, payload: dict[str, Any], *, name: str | None = None) -> dict[str, str]:
    """The `Cookie` header a browser holding this session sends, signed the way SessionMiddleware signs."""
    data = base64.b64encode(json.dumps({"user": payload}).encode())
    signed = TimestampSigner(config.session_secret.get_secret_value()).sign(data).decode()
    return {"Cookie": f"{name or config.session_cookie_name}={signed}"}


async def test_without_a_session_pages_go_to_the_login_and_everything_else_is_refused() -> None:
    async with _browser(BASE) as browser:
        page = await browser.get("/")
        api = await browser.get("/api/things")
        form = await browser.post("/form")

    assert (page.status_code, page.headers["location"]) == (303, "/auth/login")
    assert (api.status_code, api.json()) == (401, {"detail": "Not authenticated"})
    assert form.status_code == 401


async def test_the_login_flow_and_the_apps_public_paths_need_no_session_and_nothing_else_is_public() -> None:
    async with _browser(BASE) as browser:
        open_paths = [await browser.get(path) for path in ("/healthz", "/hook", "/auth/signed-out")]
        private = await browser.get("/private")

    assert [response.status_code for response in open_paths] == [200, 200, 200]
    assert SIGNED_OUT_TEXT in open_paths[-1].text
    assert private.status_code == 303


async def test_a_valid_session_is_admitted() -> None:
    async with _browser(BASE) as browser:
        page = await browser.get("/", headers=_cookie(BASE, _session_payload()))
    assert page.status_code == 200


SESSIONS_THAT_DO_NOT_HOLD = {
    "expired": _session_payload(expires_at=time.time() - 1),
    "from another issuer": _session_payload(issuer="https://other.test/"),
    "of another subject": _session_payload(subject="somebody-else"),
    "missing fields": {"issuer": ISSUER, "subject": OWNER},
    "carrying more than identity": _session_payload() | {"access_token": "leaked"},
}


@pytest.mark.parametrize("payload", SESSIONS_THAT_DO_NOT_HOLD.values(), ids=SESSIONS_THAT_DO_NOT_HOLD.keys())
async def test_a_session_that_does_not_hold_is_cleared_and_treated_as_none(payload: dict[str, Any]) -> None:
    async with _browser(BASE) as browser:
        page = await browser.get("/", headers=_cookie(BASE, payload))
        api = await browser.get("/api/things", headers=_cookie(BASE, payload))

    assert (page.status_code, page.headers["location"]) == (303, "/auth/login")
    assert page.headers["set-cookie"].startswith(f"{BASE.session_cookie_name}=null;")
    assert api.status_code == 401


async def test_without_an_allowed_subject_any_subject_the_provider_admits_holds_a_session() -> None:
    config = replace(BASE, allowed_subject=None)
    async with _browser(config) as browser:
        page = await browser.get("/", headers=_cookie(config, _session_payload(subject="somebody-else")))
    assert page.status_code == 200


@pytest.mark.parametrize(
    ("public_base_url", "name", "other_name"),
    [("http://app.test", "test-login", "__Host-test-login"), ("https://app.test", "__Host-test-login", "test-login")],
    ids=["http", "https"],
)
async def test_the_cookie_is_host_prefixed_and_secure_over_https_only(
    public_base_url: str, name: str, other_name: str
) -> None:
    config = replace(BASE, public_base_url=public_base_url)
    async with _browser(config) as browser:
        admitted = await browser.get("/", headers=_cookie(config, _session_payload(), name=name))
        ignored = await browser.get("/", headers=_cookie(config, _session_payload(), name=other_name))
        cleared = await browser.get("/", headers=_cookie(config, _session_payload(expires_at=1.0), name=name))

    assert (admitted.status_code, ignored.status_code) == (200, 303)
    assert ("secure" in cleared.headers["set-cookie"].lower()) == public_base_url.startswith("https://")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, 403), ({"Origin": "https://evil.test"}, 403), ({"Origin": BASE.public_base_url}, 200)],
    ids=["no origin", "another origin", "own origin"],
)
async def test_a_write_to_the_api_needs_the_apps_own_origin(headers: dict[str, str], expected: int) -> None:
    """SameSite=lax still lets a cross-site form post carry the cookie; the Origin check is what does not."""
    async with _browser(BASE) as browser:
        written = await browser.post("/api/things", headers=_cookie(BASE, _session_payload()) | headers)
    assert written.status_code == expected


async def test_a_read_from_another_origin_is_not_refused() -> None:
    async with _browser(BASE) as browser:
        read = await browser.get(
            "/api/things", headers=_cookie(BASE, _session_payload()) | {"Origin": "https://evil.test"}
        )
    assert read.status_code == 200


Serve = Callable[..., AbstractAsyncContextManager[LoginConfig]]


@pytest.fixture(scope="module")
def keypair() -> RSAKeyPair:
    return generate_rsa_keypair()


@pytest.fixture
def serve(keypair: RSAKeyPair) -> Serve:
    """The app and a mock IdP whose login yields `subject`, on real sockets; yields the config the app runs on."""

    @asynccontextmanager
    async def serving(
        *, subject: str = OWNER, id_token_claims: dict[str, str] | None = None, **config_overrides: Any
    ) -> AsyncIterator[LoginConfig]:
        idp_sock, app_sock = bind_free_port(), bind_free_port()
        idp_url, app_url = (f"http://127.0.0.1:{sock.getsockname()[1]}" for sock in (idp_sock, app_sock))
        private_key, public_key = keypair
        idp = build_mock_oidc_app(
            issuer_url=idp_url,
            private_key=private_key,
            public_key=public_key,
            subject=subject,
            extra_id_token_claims={"preferred_username": "test-user"} if id_token_claims is None else id_token_claims,
        )
        config = replace(BASE, issuer=idp_url, public_base_url=app_url, **config_overrides)
        app = _app()
        install_login(app, config)
        async with serve_app(idp, sock=idp_sock), serve_app(app, sock=app_sock):
            yield config

    return serving


def _origin_browser(config: LoginConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.public_base_url, headers={"Origin": config.public_base_url})


@pytest.fixture
async def owner(serve: Serve) -> AsyncIterator[httpx.AsyncClient]:
    """A browser that has completed the login."""
    async with serve() as config, _origin_browser(config) as browser:
        await browser.get("/auth/login", follow_redirects=True)
        yield browser


@pytest.mark.parametrize("session_seconds", [60, 28_800])
async def test_the_login_keeps_only_identity_and_an_expiry_no_later_than_either_limit(
    serve: Serve, session_seconds: int
) -> None:
    before = time.time()
    async with serve(session_seconds=session_seconds) as config, _origin_browser(config) as browser:
        landed = await browser.get("/auth/login", follow_redirects=True)
        api = await browser.get("/api/things")
        cookie = browser.cookies[config.session_cookie_name]
    after = time.time()

    stored = json.loads(base64.b64decode(TimestampSigner(config.session_secret.get_secret_value()).unsign(cookie)))
    session = LoginSession.model_validate(stored["user"])
    id_token_lifetime = 3600  # the mock IdP's
    assert (landed.url.path, api.status_code) == ("/", 200)
    assert set(stored["user"]) == {"issuer", "subject", "username", "expires_at"}
    assert (session.issuer, session.subject, session.username) == (config.issuer, OWNER, "test-user")
    assert before < session.expires_at <= after + min(session_seconds, id_token_lifetime)


async def test_a_subject_other_than_the_allowed_one_is_refused_without_a_session(serve: Serve) -> None:
    async with serve(subject="somebody-else") as config, _origin_browser(config) as browser:
        refused = await browser.get("/auth/login", follow_redirects=True)
        api = await browser.get("/api/things")

    assert (refused.status_code, api.status_code) == (401, 401)
    assert "This page is for its owner only." in refused.text
    assert config.session_cookie_name not in browser.cookies


async def test_without_an_allowed_subject_the_login_admits_any_subject_the_provider_does(serve: Serve) -> None:
    async with serve(subject="somebody-else", allowed_subject=None) as config, _origin_browser(config) as browser:
        await browser.get("/auth/login", follow_redirects=True)
        api = await browser.get("/api/things")
    assert api.status_code == 200


async def test_a_login_whose_claims_do_not_identify_a_user_is_refused_without_a_session(serve: Serve) -> None:
    async with serve(id_token_claims={}) as config, _origin_browser(config) as browser:
        refused = await browser.get("/auth/login", follow_redirects=True)

    assert refused.status_code == 401
    assert "Authentik sign-in failed." in refused.text
    assert config.session_cookie_name not in browser.cookies


async def test_a_failed_callback_reflects_nothing_of_the_provider_or_the_query(serve: Serve) -> None:
    async with serve() as config, _origin_browser(config) as browser:
        refused = await browser.get(
            "/auth/callback",
            params={"error": "access_denied", "error_description": "<b>reflected-text</b>", "state": "x"},
        )

    assert refused.status_code == 401
    assert "Authentik sign-in failed." in refused.text
    assert "reflected-text" not in refused.text


@pytest.mark.parametrize("session_created_log", [None, "test session created"])
async def test_a_created_session_is_logged_only_when_the_app_supplies_a_message(
    serve: Serve, caplog: pytest.LogCaptureFixture, session_created_log: str | None
) -> None:
    caplog.set_level(logging.INFO, logger="util.oidc_login")
    async with serve(session_created_log=session_created_log) as config, _origin_browser(config) as browser:
        await browser.get("/auth/login", follow_redirects=True)

    info = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert info == ([] if session_created_log is None else [session_created_log])


async def test_logout_needs_the_apps_own_origin_and_ends_the_session(owner: httpx.AsyncClient) -> None:
    refused = await owner.post("/auth/logout", headers={"Origin": "https://evil.test"})
    still_in = await owner.get("/api/things")
    out = await owner.post("/auth/logout")
    signed_out = await owner.get("/api/things")

    assert (refused.status_code, still_in.status_code) == (403, 200)
    assert (out.status_code, out.headers["location"], signed_out.status_code) == (303, "/auth/signed-out", 401)


if __name__ == "__main__":
    pytest_bazel.main()
