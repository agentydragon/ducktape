import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_bazel
from rotate import Config, Rotation, jwt_payload, remaining_hours


def _make_jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


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
    assert 9 < remaining_hours(f) < 11


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
    assert config.rotate_below_hours == 24


if __name__ == "__main__":
    pytest_bazel.main()
