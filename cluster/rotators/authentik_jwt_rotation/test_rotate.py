import base64
import json
import textwrap
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError

from cluster.rotators.authentik_jwt_rotation import rotate
from cluster.rotators.authentik_jwt_rotation.rotate import (
    Config,
    K8sSecretOutput,
    Probe,
    Rotation,
    build_secret_manifest,
    encrypt_sops_file,
    jwt_payload,
    mint_jwt,
    probe_rejects_token,
    remaining_hours,
    stamped_audiences,
    stamped_claims,
    token_audiences,
)


def _make_jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _status_client(status_code: int) -> httpx.Client:
    return _mock_client(lambda _request: httpx.Response(status_code))


def _recording_client(status_code: int, seen: list[httpx.Request], json: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, json=json)

    return _mock_client(handler)


def test_rotate_one_formats_raw_sops_file_after_encrypt(monkeypatch, tmp_path: Path):
    sops_file = tmp_path / "secrets" / "haku-k8s-jwt.yaml"
    credentials_dir = tmp_path / "creds"
    credentials_dir.mkdir()
    rotation = Rotation(
        name="haku-k8s",
        provider_slug="kubectl-sandbox-client-credentials",
        scopes="openid profile email groups",
        credentials_dir=credentials_dir,
        sops_file=sops_file,
        token_field="jwt",
    )
    token = _make_jwt({"iss": rotation.expected_issuer, "exp": 1_800_000_000, "groups": []})
    calls: list[tuple[str, object]] = []

    def fake_run(args, **_kwargs):
        calls.append(("run", list(args)))

    def fake_format(path: Path) -> None:
        calls.append(("format", path))

    monkeypatch.setattr(rotate, "mint_jwt", lambda _client, _rotation: token)
    monkeypatch.setattr(rotate.subprocess, "run", fake_run)
    monkeypatch.setattr(rotate, "prettier_format_yaml_in_place", fake_format)

    assert rotate.rotate_one(_status_client(500), rotation, Config(rotations=[])) is True
    assert calls == [("run", ["sops", "encrypt", "--indent", "2", "--in-place", str(sops_file)]), ("format", sops_file)]


def test_write_k8s_secret_formats_after_encrypt(monkeypatch, tmp_path: Path):
    out = K8sSecretOutput(
        path=tmp_path / "cluster/k8s/haku/cloud-agent-tf/haku-kube-token.sops.yaml",
        name="haku-kube-token",
        namespace="flux-system",
    )
    calls: list[tuple[str, object]] = []

    def fake_run(args, **_kwargs):
        calls.append(("run", list(args)))

    def fake_format(path: Path) -> None:
        calls.append(("format", path))

    monkeypatch.setattr(rotate.subprocess, "run", fake_run)
    monkeypatch.setattr(rotate, "prettier_format_yaml_in_place", fake_format)

    rotate.write_k8s_secret(out, token="the-jwt", exp_epoch=1_800_000_000)

    assert calls == [("run", ["sops", "encrypt", "--indent", "2", "--in-place", str(out.path)]), ("format", out.path)]


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


def test_encrypt_sops_file_uses_prettier_compatible_yaml_indent(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))

    monkeypatch.setattr(rotate.subprocess, "run", fake_run)
    path = tmp_path / "secret.yaml"
    encrypt_sops_file(path)

    assert calls == [(["sops", "encrypt", "--indent", "2", "--in-place", str(path)], {"check": True})]


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

    seen: list[httpx.Request] = []
    client = _recording_client(200, seen, json={"access_token": "minted.jwt"})
    assert mint_jwt(client, rotation) == "minted.jwt"

    [request] = seen
    assert str(request.url) == "https://auth.allegedly.works/application/o/token/"
    basic = base64.b64encode(b"provider-client:provider-secret").decode()
    assert request.headers["Authorization"] == f"Basic {basic}"
    assert dict(urllib.parse.parse_qsl(request.content.decode())) == {
        "grant_type": "client_credentials",
        "scope": "openid profile email groups",
    }


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

    seen: list[httpx.Request] = []
    client = _recording_client(200, seen, json={"access_token": "minted.jwt"})
    assert mint_jwt(client, rotation) == "minted.jwt"

    [request] = seen
    assert str(request.url) == "https://auth.allegedly.works/application/o/token/"
    assert "Authorization" not in request.headers
    assert dict(urllib.parse.parse_qsl(request.content.decode())) == {
        "grant_type": "client_credentials",
        "scope": "openid profile email groups",
        "client_id": "provider-client",
        "username": "haku-k8s",
        "password": "app-password",
    }


def test_rotation_probe_requires_k8s_secret():
    base = {
        "name": "alloy-otlp",
        "provider_slug": "alloy-otlp-client-credentials",
        "scopes": "openid profile email",
        "credentials_dir": "/creds",
        "sops_file": "secrets/alloy-otlp-bearer-token.yaml",
        "token_field": "token",
    }
    with pytest.raises(ValidationError, match="probe requires k8s_secret"):
        Rotation.model_validate(base | {"probe": {"url": "https://alloy-otlp.allegedly.works/v1/metrics"}})
    r = Rotation.model_validate(
        base
        | {
            "k8s_secret": {"path": "cluster/k8s/x.sops.yaml", "name": "alloy-otlp-bearer", "namespace": "flux-system"},
            "probe": {"url": "https://alloy-otlp.allegedly.works/v1/metrics", "method": "POST"},
        }
    )
    assert r.probe is not None
    assert r.probe.method == "POST"


def test_probe_rejects_token_only_on_auth_statuses():
    probe = Probe(url="https://example.test/x")
    assert probe_rejects_token(_status_client(401), probe, "tok") is True
    assert probe_rejects_token(_status_client(403), probe, "tok") is True
    assert probe_rejects_token(_status_client(200), probe, "tok") is False
    assert probe_rejects_token(_status_client(404), probe, "tok") is False
    # Endpoint sickness is not a credential verdict — no hourly mint churn
    # during an outage.
    assert probe_rejects_token(_status_client(503), probe, "tok") is False

    def _down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert probe_rejects_token(_mock_client(_down), probe, "tok") is False


def test_probe_rejects_token_sends_bearer_with_configured_method():
    seen: list[httpx.Request] = []
    client = _recording_client(200, seen)
    probe_rejects_token(client, Probe(url="https://example.test/v1/metrics", method="POST"), "tok")
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", "https://example.test/v1/metrics")
    assert request.headers["Authorization"] == "Bearer tok"


def _probed_rotation(tmp_path: Path) -> Rotation:
    """A rotation whose sops file is stamped fresh (2030) and that carries a probe."""
    sops_file = tmp_path / "secrets" / "alloy-otlp-bearer-token.yaml"
    sops_file.parent.mkdir()
    sops_file.write_text('expires_unencrypted: "2030-01-01T00:00:00Z"\ntoken: enc\n')
    credentials_dir = tmp_path / "creds"
    credentials_dir.mkdir()
    return Rotation(
        name="alloy-otlp",
        provider_slug="alloy-otlp-client-credentials",
        scopes="openid profile email",
        credentials_dir=credentials_dir,
        sops_file=sops_file,
        token_field="token",
        k8s_secret=K8sSecretOutput(
            path=tmp_path / "cluster/k8s/agents/alloy-otlp-bearer/alloy-otlp-bearer.sops.yaml",
            name="alloy-otlp-bearer",
            namespace="flux-system",
            token_key="token",
        ),
        probe=Probe(url="https://alloy-otlp.allegedly.works/v1/metrics", method="POST"),
    )


def _stub_mint(monkeypatch):
    token = _make_jwt(
        {"iss": "https://auth.allegedly.works/application/o/alloy-otlp-client-credentials/", "exp": 1_800_000_000}
    )
    monkeypatch.setattr(rotate, "mint_jwt", lambda _client, _rotation: token)
    monkeypatch.setattr(rotate.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rotate, "prettier_format_yaml_in_place", lambda _path: None)


def test_rotate_one_probe_rejection_forces_remint(monkeypatch, tmp_path: Path):
    rotation = _probed_rotation(tmp_path)
    _stub_mint(monkeypatch)
    monkeypatch.setattr(
        rotate, "published_secret_data", lambda _out: {"token": base64.b64encode(b"live-token").decode()}
    )
    seen: list[httpx.Request] = []
    assert rotate.rotate_one(_recording_client(401, seen), rotation, Config(rotations=[])) is True
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", "https://alloy-otlp.allegedly.works/v1/metrics")
    assert request.headers["Authorization"] == "Bearer live-token"


def test_rotate_one_probe_acceptance_keeps_fresh_token(monkeypatch, tmp_path: Path):
    rotation = _probed_rotation(tmp_path)
    monkeypatch.setattr(
        rotate, "published_secret_data", lambda _out: {"token": base64.b64encode(b"live-token").decode()}
    )
    assert rotate.rotate_one(_status_client(200), rotation, Config(rotations=[])) is False


def test_rotate_one_probe_skipped_when_secret_unreadable(monkeypatch, tmp_path: Path):
    # None = "no verdict" (API down, Flux lag): keep the fresh token rather
    # than mint-churning hourly while the publish pipeline is mid-flight.
    rotation = _probed_rotation(tmp_path)
    monkeypatch.setattr(rotate, "published_secret_data", lambda _out: None)
    seen: list[httpx.Request] = []
    assert rotate.rotate_one(_recording_client(200, seen), rotation, Config(rotations=[])) is False
    assert seen == []


def test_rotate_one_missing_token_key_forces_remint(monkeypatch, tmp_path: Path):
    # The published Secret exists but lacks the configured stringData key —
    # the class of bug a wrong token_key produces; re-mint rewrites the manifest.
    rotation = _probed_rotation(tmp_path)
    _stub_mint(monkeypatch)
    monkeypatch.setattr(rotate, "published_secret_data", lambda _out: {"jwt": base64.b64encode(b"x").decode()})
    seen: list[httpx.Request] = []
    assert rotate.rotate_one(_recording_client(200, seen), rotation, Config(rotations=[])) is True
    assert seen == []


if __name__ == "__main__":
    pytest_bazel.main()
