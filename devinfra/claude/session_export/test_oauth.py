import asyncio
import json
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.claude.claude_api.oauth_client import AUTHORIZE_URL, CLIENT_ID
from devinfra.claude.session_export.conftest import (
    PAIRED_RESPONSE,
    TEST_ACCESS_TOKEN,
    TEST_ORG_UUID,
    FakeTokenEndpoint,
    authorization_state,
    redirect_url,
)
from devinfra.claude.session_export.oauth import (
    DEFAULT_SCOPES,
    CredentialStore,
    OAuthTokenSource,
    PairedTokens,
    PairingAttempt,
    authorization_url,
    pair,
    pkce_challenge,
    redeem,
)
from util.net import pick_free_port


async def browse(port: int, *targets: str) -> list[int]:
    async with httpx.AsyncClient() as browser:
        return [(await browser.get(f"http://127.0.0.1:{port}{target}")).status_code for target in targets]


def test_validation_errors_do_not_echo_tokens(tmp_path: Path) -> None:
    tokens = json.dumps({"access_token": "test-access-secret", "refresh_token": "test-refresh-secret"})
    with pytest.raises(ValidationError) as rejected_response:
        PairedTokens.model_validate_json(tokens)  # lacks expires_in and organization
    corrupt = tmp_path / "credential.json"
    corrupt.write_text(tokens)
    with pytest.raises(ValidationError) as rejected_file:
        CredentialStore(corrupt).load()
    for failure in (rejected_response, rejected_file):
        assert "test-access-secret" not in str(failure.value)
        assert "test-refresh-secret" not in str(failure.value)


def test_pkce_challenge_matches_the_rfc_7636_example() -> None:
    assert (
        pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_authorization_url_carries_what_claudes_authorize_endpoint_requires() -> None:
    url = authorization_url(state="test-state", challenge="test-challenge", scopes=DEFAULT_SCOPES, port=54545)
    parts = urlsplit(url)
    assert parts._replace(query="").geturl() == AUTHORIZE_URL
    assert dict(parse_qsl(parts.query)) == {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": "http://localhost:54545/callback",
        "scope": "user:profile user:sessions:claude_code",
        "code_challenge": "test-challenge",
        "code_challenge_method": "S256",
        "state": "test-state",
    }


async def test_pair_saves_a_private_credential_after_the_browser_callback(tmp_path: Path) -> None:
    port, path, token_endpoint = pick_free_port(), tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE)
    announced: list[dict[str, str]] = []

    def announce(url: str) -> None:
        announced.append(dict(parse_qsl(urlsplit(url).query)))
        browsing.append(asyncio.create_task(browse(port, f"/callback?code=test-code&state={announced[0]['state']}")))

    browsing: list[asyncio.Task[list[int]]] = []
    async with asyncio.timeout(5):
        credential = await pair(
            token_endpoint.client, CredentialStore(path), scopes=DEFAULT_SCOPES, port=port, announce=announce
        )

    assert await browsing[0] == [200]
    (body,) = token_endpoint.bodies
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "test-code"
    assert body["state"] == announced[0]["state"]
    assert pkce_challenge(body["code_verifier"]) == announced[0]["code_challenge"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert CredentialStore(path).load() == credential
    assert credential.access_token.get_secret_value() == TEST_ACCESS_TOKEN
    assert credential.scopes == {"user:profile"}  # what the server reports as granted, not what was asked
    assert credential.organization_uuid == TEST_ORG_UUID
    assert abs(credential.expires_at - datetime.now(UTC) - timedelta(hours=1)) < timedelta(seconds=30)


async def test_a_pasted_redirect_url_is_redeemed_like_a_callback() -> None:
    attempt, token_endpoint = (
        PairingAttempt.start(scopes=DEFAULT_SCOPES, port=54545),
        FakeTokenEndpoint(PAIRED_RESPONSE),
    )
    pasted = f" {redirect_url(attempt.state)}\n"  # as copied from the address bar
    credential = await redeem(token_endpoint.client, attempt, attempt.code_from_redirect(pasted))

    (body,) = token_endpoint.bodies
    assert (body["code"], body["state"], body["redirect_uri"]) == (
        "test-code",
        attempt.state,
        "http://localhost:54545/callback",
    )
    assert pkce_challenge(body["code_verifier"]) == dict(parse_qsl(urlsplit(attempt.url).query))["code_challenge"]
    assert credential.organization_uuid == TEST_ORG_UUID


@pytest.mark.parametrize(
    ("redirect", "error"),
    [
        (redirect_url("another-attempt"), "state differs"),
        ("http://localhost:54545/callback?code=test-code", "state differs"),
        ("test-code", "state differs"),
        ("http://localhost:54545/callback?error=access_denied&state={state}", "access_denied"),
        ("http://localhost:54545/callback?state={state}", "no code returned"),
    ],
)
def test_a_pasted_redirect_url_that_is_not_this_attempts_approval_is_refused(redirect: str, error: str) -> None:
    attempt = PairingAttempt.start(scopes=DEFAULT_SCOPES, port=54545)
    with pytest.raises(ValueError, match=error) as refused:
        attempt.code_from_redirect(redirect.format(state=attempt.state))
    assert "test-code" not in str(refused.value)


async def test_pair_refuses_a_callback_with_the_wrong_state_and_completes_on_the_right_one(tmp_path: Path) -> None:
    port, token_endpoint = pick_free_port(), FakeTokenEndpoint(PAIRED_RESPONSE)
    browsing: list[asyncio.Task[list[int]]] = []

    def announce(url: str) -> None:
        state = authorization_state(url)
        browsing.append(
            asyncio.create_task(
                browse(port, "/callback?code=stray&state=wrong", f"/callback?code=test-code&state={state}")
            )
        )

    async with asyncio.timeout(5):
        await pair(
            token_endpoint.client,
            CredentialStore(tmp_path / "credential.json"),
            scopes=DEFAULT_SCOPES,
            port=port,
            announce=announce,
        )

    assert await browsing[0] == [400, 200]
    assert [body["code"] for body in token_endpoint.bodies] == ["test-code"]


async def test_pair_fails_when_authorization_is_refused_and_saves_nothing(tmp_path: Path) -> None:
    port, path, token_endpoint = pick_free_port(), tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE)

    browsing: list[asyncio.Task[list[int]]] = []

    def announce(url: str) -> None:
        state = authorization_state(url)
        browsing.append(asyncio.create_task(browse(port, f"/callback?error=access_denied&state={state}")))

    with pytest.raises(ValueError, match="access_denied"):
        async with asyncio.timeout(5):
            await pair(
                token_endpoint.client, CredentialStore(path), scopes=DEFAULT_SCOPES, port=port, announce=announce
            )
    assert await browsing[0] == [400]
    assert not path.exists()
    assert not token_endpoint.bodies


async def test_pair_times_out_without_a_callback_and_saves_nothing(tmp_path: Path) -> None:
    path, token_endpoint = tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await pair(
                token_endpoint.client,
                CredentialStore(path),
                scopes=DEFAULT_SCOPES,
                port=pick_free_port(),
                announce=lambda _: None,
            )
    assert not path.exists()
    assert not token_endpoint.bodies


async def test_token_source_refreshes_an_expiring_token_once_and_persists_the_rotation(
    credential_store: Callable[..., CredentialStore],
) -> None:
    store = credential_store(expires_in=timedelta(minutes=1))  # inside the refresh skew
    token_endpoint = FakeTokenEndpoint(
        {"access_token": "test-access-2", "refresh_token": "test-refresh-2", "expires_in": 28800}
    )
    source = OAuthTokenSource(store, token_endpoint.client)

    tokens = await asyncio.gather(*(source.access_token() for _ in range(5)))

    assert tokens == ["test-access-2"] * 5
    (body,) = token_endpoint.bodies
    assert (body["grant_type"], body["refresh_token"]) == ("refresh_token", "test-refresh-1")
    reloaded = store.load()
    assert reloaded.refresh_token.get_secret_value() == "test-refresh-2"
    assert reloaded.access_token.get_secret_value() == "test-access-2"


async def test_a_refused_refresh_carries_the_oauth_error_from_the_token_endpoint(
    credential_store: Callable[..., CredentialStore],
) -> None:
    token_endpoint = FakeTokenEndpoint({"error": "invalid_grant"}, status=400)
    store = credential_store(expires_in=timedelta(seconds=-1))
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await OAuthTokenSource(store, token_endpoint.client).access_token()
    assert any("invalid_grant" in note for note in refused.value.__notes__)


async def test_token_source_keeps_a_valid_token_without_touching_the_network(
    credential_store: Callable[..., CredentialStore],
) -> None:
    token_endpoint = FakeTokenEndpoint({})
    assert await OAuthTokenSource(credential_store(), token_endpoint.client).access_token() == TEST_ACCESS_TOKEN
    assert not token_endpoint.bodies


async def test_refresh_keeps_the_refresh_token_when_the_server_does_not_rotate_it(
    credential_store: Callable[..., CredentialStore],
) -> None:
    store = credential_store(expires_in=timedelta(seconds=-1))
    token_endpoint = FakeTokenEndpoint({"access_token": "test-access-2", "expires_in": 28800})
    await OAuthTokenSource(store, token_endpoint.client).access_token()
    assert store.load().refresh_token.get_secret_value() == "test-refresh-1"


if __name__ == "__main__":
    pytest_bazel.main()
