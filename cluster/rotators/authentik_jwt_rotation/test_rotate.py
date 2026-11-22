import base64
import json
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from cluster.rotators.authentik_jwt_rotation import rotate
from cluster.rotators.authentik_jwt_rotation.config import Config, K8sSecretOutput, Probe, Rotation
from cluster.rotators.authentik_jwt_rotation.rotate import mint_jwt, token_audiences


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


def test_token_audiences_normalizes_string_list_and_missing():
    assert token_audiences({"aud": "one"}) == ["one"]
    assert token_audiences({"aud": ["one", "two"]}) == ["one", "two"]
    assert token_audiences({}) == []


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
    with pytest.raises(ValidationError):
        Rotation.model_validate(base | {"probe": {"url": "https://alloy-otlp.example.test/v1/metrics"}})
    r = Rotation.model_validate(
        base
        | {
            "k8s_secret": {"path": "cluster/k8s/x.sops.yaml", "name": "alloy-otlp-bearer", "namespace": "flux-system"},
            "probe": {"url": "https://alloy-otlp.example.test/v1/metrics", "method": "POST"},
        }
    )
    assert r.probe is not None
    assert r.probe.method == "POST"


@pytest.fixture
def probed_rotation(tmp_path: Path) -> Rotation:
    """A rotation whose SOPS file is stamped fresh and that carries a probe."""
    sops_file = tmp_path / "secrets" / "alloy-otlp-bearer-token.yaml"
    sops_file.parent.mkdir()
    expires = datetime.now(UTC) + timedelta(days=30)
    sops_file.write_text(f'expires_unencrypted: "{expires:%Y-%m-%dT%H:%M:%SZ}"\ntoken: enc\n')
    return Rotation(
        name="alloy-otlp",
        provider_slug="alloy-otlp-client-credentials",
        scopes="openid profile email",
        credentials_dir=tmp_path / "creds",
        sops_file=sops_file,
        token_field="token",
        k8s_secret=K8sSecretOutput(
            path=tmp_path / "cluster/k8s/agents/alloy-otlp-bearer/alloy-otlp-bearer.sops.yaml",
            name="alloy-otlp-bearer",
            namespace="flux-system",
            token_key="token",
        ),
        probe=Probe(url="https://alloy-otlp.example.test/v1/metrics", method="POST"),
    )


def _stub_mint(monkeypatch):
    token = _make_jwt(
        {"iss": "https://auth.allegedly.works/application/o/alloy-otlp-client-credentials/", "exp": 1_800_000_000}
    )
    monkeypatch.setattr(rotate, "mint_jwt", lambda _client, _rotation: token)
    monkeypatch.setattr(rotate, "write_sops_yaml", lambda **_kwargs: None)


@pytest.mark.parametrize(
    ("status", "remint"),
    [(401, True), (403, True), (200, False), (404, False), (503, False), pytest.param(None, False, id="unreachable")],
)
def test_rotate_one_remints_only_if_the_probe_rejects_the_published_bearer(
    monkeypatch, probed_rotation: Rotation, status: int | None, remint: bool
) -> None:
    _stub_mint(monkeypatch)
    monkeypatch.setattr(
        rotate, "published_secret_data", lambda _out: {"token": base64.b64encode(b"live-token").decode()}
    )
    seen: list[httpx.Request] = []

    def probe(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status is None:
            raise httpx.ConnectError("down")
        return httpx.Response(status)

    with _mock_client(probe) as client:
        assert rotate.rotate_one(client, probed_rotation, Config(rotations=[])) is remint
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", "https://alloy-otlp.example.test/v1/metrics")
    assert request.headers["Authorization"] == "Bearer live-token"


@pytest.mark.parametrize(
    ("data", "remint"),
    [pytest.param(None, False, id="unreadable-secret"), pytest.param({"jwt": "eA=="}, True, id="missing-token-key")],
)
def test_rotate_one_without_a_published_bearer_skips_the_probe(
    monkeypatch, probed_rotation: Rotation, data: dict[str, str] | None, remint: bool
) -> None:
    _stub_mint(monkeypatch)
    monkeypatch.setattr(rotate, "published_secret_data", lambda _out: data)
    seen: list[httpx.Request] = []
    assert rotate.rotate_one(_recording_client(200, seen), probed_rotation, Config(rotations=[])) is remint
    assert seen == []


@pytest.mark.parametrize("expected", ["audience", "claim"])
def test_rotated_outputs_satisfy_the_next_freshness_check(monkeypatch, probed_rotation: Rotation, expected: str):
    probed_rotation.probe = None
    assert probed_rotation.k8s_secret is not None
    expiry = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
    claims = {
        "iss": probed_rotation.expected_issuer,
        "exp": expiry,
        "aud": "test-audience",
        "email": "test-user@example.test",
    }
    if expected == "audience":
        probed_rotation.expected_audiences = ["test-audience"]
    else:
        probed_rotation.expected_claims = {"email": "test-user@example.test"}

    token = _make_jwt(claims)
    monkeypatch.setattr(rotate, "mint_jwt", lambda _client, _rotation: token)

    def write_yaml(*, path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document))

    monkeypatch.setattr(rotate, "write_sops_yaml", write_yaml)
    client = _status_client(200)
    config = Config(rotations=[])
    # The existing token is fresh by age, but predates the newly required audience or claim.
    assert rotate.rotate_one(client, probed_rotation, config)
    secret = yaml.safe_load(probed_rotation.k8s_secret.path.read_text())
    assert secret["stringData"] == {"token": token, "token-exp": str(expiry)}
    assert secret["metadata"]["annotations"]["description"]
    assert yaml.safe_load(probed_rotation.sops_file.read_text())[probed_rotation.token_field] == token
    assert not rotate.rotate_one(client, probed_rotation, config)


if __name__ == "__main__":
    pytest_bazel.main()
