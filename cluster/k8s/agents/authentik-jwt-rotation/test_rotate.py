import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError
from rotate import Config, Rotation, jwt_payload, mint_jwt, remaining_hours


def _make_jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _RecordingClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse({"access_token": "minted.jwt"})


def test_jwt_payload_decodes_unpadded_base64url():
    claims = {"iss": "https://auth.allegedly.works/application/o/x/", "groups": ["some-group"], "exp": 123}
    assert jwt_payload(_make_jwt(claims)) == claims


def test_remaining_hours_missing_file_is_none(tmp_path: Path):
    assert remaining_hours(tmp_path / "absent.yaml") is None


def test_remaining_hours_unstamped_file_is_none(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text("jwt: abc\n")
    assert remaining_hours(f) is None


def test_remaining_hours_reads_unencrypted_expiry(tmp_path: Path):
    expires = datetime.now(UTC) + timedelta(hours=10)
    f = tmp_path / "t.yaml"
    f.write_text(f'expires_unencrypted: "{expires:%Y-%m-%dT%H:%M:%SZ}"\njwt: abc\n')
    remaining = remaining_hours(f)
    assert remaining is not None
    assert 9 < remaining < 11


def test_rotation_expected_issuer_derived_from_slug():
    r = Rotation(
        name="x",
        provider_slug="kubectl-sandbox-client-credentials",
        scopes="openid",
        credentials_dir=Path("/creds"),
        sops_file=Path("secrets/x.yaml"),
        token_field="jwt",
    )
    assert r.expected_issuer == "https://auth.allegedly.works/application/o/kubectl-sandbox-client-credentials/"


def test_config_parses_exchange_and_group_fields():
    config = Config.model_validate(
        {
            "rotations": [
                {
                    "name": "alloy-otlp",
                    "provider_slug": "alloy-otlp-client-credentials",
                    "scopes": "openid profile email",
                    "exchange_scopes": "openid profile email ak_proxy",
                    "credentials_dir": "/var/run/secrets/authentik/alloy-otlp",
                    "sops_file": "secrets/alloy-otlp-bearer-token.yaml",
                    "token_field": "token",
                }
            ]
        }
    )
    (rotation,) = config.rotations
    assert rotation.exchange_scopes == "openid profile email ak_proxy"
    assert rotation.expected_group is None
    assert rotation.credential_mode == "client_secret"
    assert config.rotate_below_hours == 24


def test_config_rejects_unknown_credential_mode():
    with pytest.raises(ValidationError, match="credential_mode"):
        Config.model_validate(
            {
                "rotations": [
                    {
                        "name": "haku-k8s",
                        "provider_slug": "kubectl-sandbox-client-credentials",
                        "scopes": "openid profile email groups",
                        "credential_mode": "strip-suffix",
                        "credentials_dir": "/var/run/secrets/authentik/haku-k8s",
                        "sops_file": "secrets/haku-k8s-jwt.yaml",
                        "token_field": "jwt",
                    }
                ]
            }
        )


def test_mint_jwt_default_mode_uses_provider_client_secret(tmp_path: Path):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "client_id").write_text("provider-client\n")
    (creds / "client_secret").write_text("provider-secret\n")
    rotation = Rotation(
        name="claude-web-k8s",
        provider_slug="kubectl-sandbox-client-credentials",
        scopes="openid profile email groups",
        credentials_dir=creds,
        sops_file=Path("secrets/claude-web-k8s-jwt.yaml"),
        token_field="jwt",
    )

    client = _RecordingClient()
    assert mint_jwt(client, rotation) == "minted.jwt"

    [(url, kwargs)] = client.calls
    assert url == "https://auth.allegedly.works/application/o/token/"
    assert kwargs["auth"] == ("provider-client", "provider-secret")
    assert kwargs["data"] == {"grant_type": "client_credentials", "scope": "openid profile email groups"}


def test_mint_jwt_user_password_mode_uses_form_credentials(tmp_path: Path):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "client_id").write_text("provider-client\n")
    (creds / "username").write_text("haku-k8s\n")
    (creds / "password").write_text("app-password\n")
    rotation = Rotation(
        name="haku-k8s",
        provider_slug="kubectl-sandbox-client-credentials",
        scopes="openid profile email groups",
        credential_mode="user_password",
        credentials_dir=creds,
        sops_file=Path("secrets/haku-k8s-jwt.yaml"),
        token_field="jwt",
    )

    client = _RecordingClient()
    assert mint_jwt(client, rotation) == "minted.jwt"

    [(url, kwargs)] = client.calls
    assert url == "https://auth.allegedly.works/application/o/token/"
    assert "auth" not in kwargs
    assert kwargs["data"] == {
        "grant_type": "client_credentials",
        "scope": "openid profile email groups",
        "client_id": "provider-client",
        "username": "haku-k8s",
        "password": "app-password",
    }


if __name__ == "__main__":
    pytest_bazel.main()
