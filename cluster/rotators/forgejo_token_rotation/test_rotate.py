from datetime import UTC, datetime
from pathlib import Path

import pytest_bazel

from cluster.rotators.forgejo_token_rotation.rotate import (
    FULL_ACCOUNT_SCOPES,
    ForgejoCredentials,
    RotatedToken,
    Rotation,
    TeaSecretOutput,
    build_secret_manifest,
    mint_token,
    should_rotate,
    tea_config_yaml,
    tokens_to_prune,
)


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _RecordingClient:
    def __init__(self):
        self.posts = []

    def post(self, url: str, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeResponse(
            {
                "id": 12,
                "name": kwargs["json"]["name"],
                "sha1": "abcd1234token",
                "token_last_eight": "34token",
                "scopes": kwargs["json"]["scopes"],
                "repositories": None,
            },
            status_code=201,
        )


def test_default_scopes_are_full_non_admin_write_set():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    assert r.scopes == FULL_ACCOUNT_SCOPES


def test_should_rotate_missing_stamps():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    due, reason = should_rotate(r, {}, [], now=datetime(2026, 7, 1, tzinfo=UTC))
    assert due
    assert "no existing" in reason


def test_should_skip_fresh_present_token():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    stamps = {
        "rotated_at_unencrypted": "2026-07-01T00:00:00Z",
        "rotate_after_days_unencrypted": 30,
        "repository_access_unencrypted": "all",
        "scopes_unencrypted": FULL_ACCOUNT_SCOPES,
        "token_id_unencrypted": 12,
        "token_name_unencrypted": "forgejo-tea-haku-20260701000000",
        "token_last_eight_unencrypted": "34token",
    }
    tokens = [{"id": 12, "name": "forgejo-tea-haku-20260701000000", "token_last_eight": "34token"}]
    due, reason = should_rotate(r, stamps, tokens, now=datetime(2026, 7, 2, tzinfo=UTC))
    assert not due
    assert "fresh until" in reason


def test_should_rotate_when_scopes_change():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    stamps = {
        "rotated_at_unencrypted": "2026-07-01T00:00:00Z",
        "rotate_after_days_unencrypted": 30,
        "repository_access_unencrypted": "all",
        "scopes_unencrypted": ["write:repository"],
        "token_id_unencrypted": 12,
    }
    due, reason = should_rotate(r, stamps, [{"id": 12}], now=datetime(2026, 7, 2, tzinfo=UTC))
    assert due
    assert reason == "scope set changed"


def test_should_rotate_when_token_age_reaches_interval():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    stamps = {
        "rotated_at_unencrypted": "2026-07-01T00:00:00Z",
        "rotate_after_days_unencrypted": 30,
        "repository_access_unencrypted": "all",
        "scopes_unencrypted": FULL_ACCOUNT_SCOPES,
        "token_id_unencrypted": 12,
    }
    due, reason = should_rotate(r, stamps, [{"id": 12}], now=datetime(2026, 7, 31, tzinfo=UTC))
    assert due
    assert reason == "token age reached 30d"


def test_should_rotate_when_stamped_token_missing_from_forgejo():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    stamps = {
        "rotated_at_unencrypted": "2026-07-01T00:00:00Z",
        "rotate_after_days_unencrypted": 30,
        "repository_access_unencrypted": "all",
        "scopes_unencrypted": FULL_ACCOUNT_SCOPES,
        "token_id_unencrypted": 12,
    }
    due, reason = should_rotate(r, stamps, [{"id": 13}], now=datetime(2026, 7, 2, tzinfo=UTC))
    assert due
    assert reason == "stamped token is not present in Forgejo"


def test_tea_config_yaml_matches_upstream_config_shape():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    creds = ForgejoCredentials(
        username="haku",
        password="secret",
        api_url="http://forgejo-http.forgejo:3000",
        tea_url="https://git.allegedly.works",
    )
    rendered = tea_config_yaml(r, creds, "token-value")
    assert "logins:" in rendered
    assert "name: forgejo" in rendered
    assert "url: https://git.allegedly.works" in rendered
    assert "token: token-value" in rendered
    assert "default: true" in rendered
    assert "version_check: false" in rendered
    assert "user: haku" in rendered


def test_secret_manifest_carries_config_and_raw_token():
    out = TeaSecretOutput(
        path=Path("cluster/k8s/haku/agent-worker/haku-forgejo-tea.sops.yaml"),
        name="haku-forgejo-tea",
        namespace="haku-sandbox",
    )
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"), tea_secret=out)
    creds = ForgejoCredentials("haku", "secret", "http://forgejo-http.forgejo:3000", "https://git.allegedly.works")
    manifest = build_secret_manifest(
        out,
        r,
        creds,
        {"id": 12, "name": "forgejo-tea-haku-20260701", "sha1": "token-value", "token_last_eight": "en-value"},
    )
    assert manifest["metadata"]["name"] == "haku-forgejo-tea"
    assert manifest["metadata"]["namespace"] == "haku-sandbox"
    assert manifest["stringData"]["token"] == "token-value"
    assert manifest["stringData"]["username"] == "haku"
    assert "config.yml" in manifest["stringData"]


def test_mint_token_omits_repositories_for_full_account_access():
    r = Rotation(
        name="haku",
        token_prefix="forgejo-tea-haku",
        credentials_dir=Path("/creds"),
        sops_file=Path("secrets/haku.yaml"),
    )
    creds = ForgejoCredentials("haku", "secret", "http://forgejo-http.forgejo:3000", "https://git.allegedly.works")
    client = _RecordingClient()
    data = mint_token(client, r, creds, now=datetime(2026, 7, 1, 1, 2, 3, 456789, tzinfo=UTC))
    assert data["name"] == "forgejo-tea-haku-20260701010203456789"
    assert client.posts[0][1]["auth"] == ("haku", "secret")
    assert client.posts[0][1]["json"] == {"name": data["name"], "scopes": FULL_ACCOUNT_SCOPES}


def test_tokens_to_prune_keeps_current_and_newest_previous():
    r = Rotation(
        name="haku",
        token_prefix="forgejo-tea-haku",
        credentials_dir=Path("/creds"),
        sops_file=Path("secrets/haku.yaml"),
        keep_previous=1,
    )
    creds = ForgejoCredentials("haku", "secret", "http://forgejo-http.forgejo:3000", "https://git.allegedly.works")
    current = RotatedToken(
        rotation=r,
        credentials=creds,
        token_id=4,
        token_name="forgejo-tea-haku-20260704000000",
        token_last_eight="current",
    )
    tokens = [
        {"id": 4, "name": "forgejo-tea-haku-20260704000000"},
        {"id": 3, "name": "forgejo-tea-haku-20260703000000"},
        {"id": 2, "name": "forgejo-tea-haku-20260702000000"},
        {"id": 1, "name": "manual-token"},
    ]
    assert tokens_to_prune(r, current, tokens) == [{"id": 2, "name": "forgejo-tea-haku-20260702000000"}]


if __name__ == "__main__":
    pytest_bazel.main()
