import base64
import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError
from rotate import (
    Config,
    K8sSecretOutput,
    Rotation,
    build_secret_manifest,
    jwt_payload,
    mint_jwt,
    remaining_hours,
    stamped_audiences,
    stamped_claims,
    token_audiences,
)


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
    f.write_text(
        textwrap.dedent(f"""\
            expires_unencrypted: "{expires:%Y-%m-%dT%H:%M:%SZ}"
            jwt: abc
            """)
    )
    remaining = remaining_hours(f)
    assert remaining is not None
    assert 9 < remaining < 11


def test_token_audiences_normalizes_string_list_and_missing():
    assert token_audiences({"aud": "one"}) == ["one"]
    assert token_audiences({"aud": ["one", "two"]}) == ["one", "two"]
    assert token_audiences({}) == []


def test_stamped_audiences_reads_yaml_list(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text(
        textwrap.dedent("""\
            expires_unencrypted: "2030-01-01T00:00:00Z"
            audiences_unencrypted:
              - a
              - b
            jwt: abc
            """)
    )
    assert stamped_audiences(f) == ["a", "b"]


def test_stamped_audiences_absent_is_none(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text("jwt: abc\n")
    assert stamped_audiences(f) is None
    assert stamped_audiences(tmp_path / "absent.yaml") is None


def test_stamped_claims_reads_yaml_dict(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text(
        textwrap.dedent("""\
            expires_unencrypted: "2030-01-01T00:00:00Z"
            claims_unencrypted:
              email: haku@allegedly.works
            jwt: abc
            """)
    )
    assert stamped_claims(f) == {"email": "haku@allegedly.works"}


def test_stamped_claims_absent_is_none(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text("jwt: abc\n")
    assert stamped_claims(f) is None
    assert stamped_claims(tmp_path / "absent.yaml") is None


def test_rotation_expected_audiences_defaults_none_and_parses_list():
    base = {
        "name": "haku-k8s",
        "provider_slug": "kubectl-sandbox-client-credentials",
        "scopes": "openid profile email groups",
        "credentials_dir": "/creds",
        "sops_file": "secrets/haku-k8s-jwt.yaml",
        "token_field": "jwt",
    }
    assert Rotation.model_validate(base).expected_audiences is None
    with_aud = Rotation.model_validate(
        base | {"expected_audiences": ["kubectl-sandbox-client-credentials", "kubectl-passthrough-mcp"]}
    )
    assert with_aud.expected_audiences == ["kubectl-sandbox-client-credentials", "kubectl-passthrough-mcp"]


def test_rotation_expected_claims_defaults_none_and_parses_dict():
    base = {
        "name": "haku-mail",
        "provider_slug": "stalwart-haku",
        "scopes": "openid profile email",
        "credentials_dir": "/creds",
        "sops_file": "secrets/haku-mail-jwt.yaml",
        "token_field": "jwt",
    }
    assert Rotation.model_validate(base).expected_claims is None
    with_claims = Rotation.model_validate(base | {"expected_claims": {"email": "haku@allegedly.works"}})
    assert with_claims.expected_claims == {"email": "haku@allegedly.works"}


def test_rotation_k8s_secret_defaults_none_and_parses():
    base = {
        "name": "haku-k8s",
        "provider_slug": "kubectl-sandbox-client-credentials",
        "scopes": "openid profile email groups",
        "credentials_dir": "/creds",
        "sops_file": "secrets/haku-k8s-jwt.yaml",
        "token_field": "jwt",
    }
    assert Rotation.model_validate(base).k8s_secret is None
    r = Rotation.model_validate(
        base
        | {
            "k8s_secret": {
                "path": "cluster/k8s/haku/cloud-agent-tf/haku-kube-token.sops.yaml",
                "name": "haku-cloud-kube-token",
                "namespace": "flux-system",
            }
        }
    )
    assert isinstance(r.k8s_secret, K8sSecretOutput)
    assert r.k8s_secret.token_key == "jwt"  # default
    assert r.k8s_secret.exp_key == "token-exp"  # default
    assert r.k8s_secret.namespace == "flux-system"


def test_build_secret_manifest_carries_token_exp_under_configured_keys():
    out = K8sSecretOutput(
        path=Path("cluster/k8s/x.sops.yaml"), name="haku-cloud-grocy-sf-token", namespace="flux-system"
    )
    manifest = build_secret_manifest(out, token="the-jwt", exp_epoch=1750000000)
    assert manifest["stringData"] == {"jwt": "the-jwt", "token-exp": "1750000000"}
    assert list(manifest["metadata"]["annotations"]) == ["description"]


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
