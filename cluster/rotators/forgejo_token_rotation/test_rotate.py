import base64
import json
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from cluster.rotators.forgejo_token_rotation import rotate
from cluster.rotators.forgejo_token_rotation.config import FULL_ACCOUNT_SCOPES, Config, Rotation, TeaSecretOutput
from cluster.rotators.forgejo_token_rotation.rotate import (
    ForgejoCredentials,
    RotatedToken,
    build_secret_manifest,
    mint_token,
    should_rotate,
    tea_config_yaml,
    tokens_to_prune,
)


def _minting_client(seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": 12,
                "name": body["name"],
                "sha1": "abcd1234token",
                "token_last_eight": "34token",
                "scopes": body["scopes"],
                "repositories": None,
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_build_tea_secret_preserves_configured_metadata_annotations(tmp_path: Path):
    output = TeaSecretOutput(
        path=tmp_path / "secret.sops.yaml",
        name="forgejo-token",
        namespace="target",
        annotations={"reflector.v1.k8s.emberstack.com/reflection-allowed": "true"},
    )
    rotation = Rotation(
        name="test", credentials_dir=Path("/creds"), sops_file=Path("secrets/test.yaml"), tea_secret=output
    )

    manifest = build_secret_manifest(
        output,
        rotation,
        ForgejoCredentials(
            username="test", password="not-a-secret", api_url="http://forgejo", tea_url="https://forgejo"
        ),
        {"sha1": "token", "name": "forgejo-token-test", "token_last_eight": "token"},
    )

    annotations = manifest["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed"] == "true"
    assert "description" in annotations


def test_minted_token_requests_full_non_admin_write_scope_set():
    """Boundary pin: the scope grant a default rotation actually requests from Forgejo.

    Written as literals so any change to the granted access level fails here and gets
    reviewed as a permissions change, not absorbed by a constant edit.
    """
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    creds = ForgejoCredentials("test-user", "test-secret", "http://forgejo.test:3000", "https://git.test")
    seen: list[httpx.Request] = []
    mint_token(_minting_client(seen), r, creds, now=datetime(2026, 7, 1, tzinfo=UTC))
    assert json.loads(seen[0].content)["scopes"] == [
        "write:activitypub",
        "write:issue",
        "write:misc",
        "write:notification",
        "write:organization",
        "write:package",
        "write:repository",
        "write:user",
    ]


@pytest.mark.parametrize(
    ("overrides", "token_id", "day", "due"),
    [
        pytest.param(None, 12, 2, True, id="missing-stamp"),
        pytest.param({}, 12, 2, False, id="fresh"),
        pytest.param({"scopes_unencrypted": ["write:repository"]}, 12, 2, True, id="scope-change"),
        pytest.param({}, 12, 31, True, id="age-boundary"),
        pytest.param({}, 13, 2, True, id="missing-token"),
    ],
)
def test_should_rotate_due_to_stamps_age_or_token_presence(overrides, token_id: int, day: int, due: bool) -> None:
    rotation = Rotation(name="test-account", credentials_dir=Path("/creds"), sops_file=Path("secrets/test.yaml"))
    stamps = {
        "rotated_at_unencrypted": "2026-07-01T00:00:00Z",
        "rotate_after_days_unencrypted": 30,
        "repository_access_unencrypted": "all",
        "scopes_unencrypted": FULL_ACCOUNT_SCOPES,
        "token_id_unencrypted": 12,
        "token_name_unencrypted": "test-token-12",
        "token_last_eight_unencrypted": "testtail",
    }
    tokens = [{"id": token_id, "name": f"test-token-{token_id}", "token_last_eight": "testtail"}]
    rotate_due, _ = should_rotate(
        rotation, (stamps | overrides) if overrides is not None else {}, tokens, now=datetime(2026, 7, day, tzinfo=UTC)
    )
    assert rotate_due is due


def test_tea_config_yaml_matches_upstream_config_shape():
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"))
    creds = ForgejoCredentials(
        username="test-user", password="test-secret", api_url="http://forgejo.test:3000", tea_url="https://git.test"
    )
    rendered = tea_config_yaml(r, creds, "token-value")
    assert "logins:" in rendered
    assert "name: forgejo" in rendered
    assert "url: https://git.test" in rendered
    assert "token: token-value" in rendered
    assert "default: true" in rendered
    assert "version_check: false" in rendered
    assert "user: test-user" in rendered


def test_secret_manifest_carries_config_and_raw_token():
    out = TeaSecretOutput(
        path=Path("cluster/k8s/haku/managed-agent/haku-forgejo-tea.sops.yaml"),
        name="haku-forgejo-tea",
        namespace="haku-sandbox",
    )
    r = Rotation(name="haku", credentials_dir=Path("/creds"), sops_file=Path("secrets/haku.yaml"), tea_secret=out)
    creds = ForgejoCredentials("test-user", "test-secret", "http://forgejo.test:3000", "https://git.test")
    manifest = build_secret_manifest(
        out,
        r,
        creds,
        {"id": 12, "name": "forgejo-tea-haku-20260701", "sha1": "token-value", "token_last_eight": "en-value"},
    )
    assert manifest["metadata"]["name"] == "haku-forgejo-tea"
    assert manifest["metadata"]["namespace"] == "haku-sandbox"
    assert manifest["stringData"]["token"] == "token-value"
    assert manifest["stringData"]["username"] == "test-user"
    assert "config.yml" in manifest["stringData"]


def test_mint_token_omits_repositories_for_full_account_access():
    r = Rotation(
        name="haku",
        token_prefix="forgejo-tea-haku",
        credentials_dir=Path("/creds"),
        sops_file=Path("secrets/haku.yaml"),
    )
    creds = ForgejoCredentials("test-user", "test-secret", "http://forgejo.test:3000", "https://git.test")
    seen: list[httpx.Request] = []
    data = mint_token(_minting_client(seen), r, creds, now=datetime(2026, 7, 1, 1, 2, 3, 456789, tzinfo=UTC))
    assert data["name"] == "forgejo-tea-haku-20260701010203456789"
    assert seen[0].headers["Authorization"] == "Basic " + base64.b64encode(b"test-user:test-secret").decode()
    assert json.loads(seen[0].content) == {"name": data["name"], "scopes": FULL_ACCOUNT_SCOPES}


def test_tokens_to_prune_keeps_current_and_newest_previous():
    r = Rotation(
        name="haku",
        token_prefix="forgejo-tea-haku",
        credentials_dir=Path("/creds"),
        sops_file=Path("secrets/haku.yaml"),
        keep_previous=1,
    )
    creds = ForgejoCredentials("test-user", "test-secret", "http://forgejo.test:3000", "https://git.test")
    current = RotatedToken(rotation=r, credentials=creds, token_id=4, token_name="forgejo-tea-haku-20260704000000")
    tokens = [
        {"id": 4, "name": "forgejo-tea-haku-20260704000000"},
        {"id": 3, "name": "forgejo-tea-haku-20260703000000"},
        {"id": 2, "name": "forgejo-tea-haku-20260702000000"},
        {"id": 1, "name": "manual-token"},
    ]
    assert tokens_to_prune(r, current, tokens) == [{"id": 2, "name": "forgejo-tea-haku-20260702000000"}]


@pytest.mark.parametrize("pushed", [True, False, None], ids=["published", "unchanged", "push-rejected"])
def test_only_published_rotations_prune_even_when_another_rotation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pushed: bool | None
) -> None:
    broken, healthy = [
        Rotation(name=name, credentials_dir=tmp_path / name, sops_file=tmp_path / f"{name}.yaml")
        for name in ("broken", "healthy")
    ]
    config = tmp_path / "config.yaml"
    config.write_text(Config(rotations=[broken, healthy]).model_dump_json())
    pat = tmp_path / "pat"
    pat.write_text("test-pat")
    credentials = ForgejoCredentials("test-user", "test-secret", "https://forgejo.test", "https://git.test")
    events: list[str] = []

    def rotate_one(_client: httpx.Client, rotation: Rotation) -> RotatedToken:
        if rotation.name == broken.name:
            raise RuntimeError("Forgejo rejected the rotation")
        return RotatedToken(rotation=rotation, credentials=credentials, token_id=2, token_name="test-token")

    def push_outputs(paths: Iterable[Path], **_kwargs: object) -> bool:
        assert list(paths) == [healthy.sops_file]
        events.append("push")
        if pushed is None:
            raise subprocess.CalledProcessError(1, "git push")
        return pushed

    monkeypatch.setattr(rotate, "enter_sparse_checkout", lambda *_args: None)
    monkeypatch.setattr(rotate, "rotate_one", rotate_one)
    monkeypatch.setattr(rotate, "push_outputs", push_outputs)
    monkeypatch.setattr(rotate, "prune_old_tokens", lambda *_args: events.append("prune"))

    if pushed is None:
        with pytest.raises(subprocess.CalledProcessError):
            rotate.main(config, pat)
    else:
        with pytest.raises(SystemExit, match="rotations failed: broken"):
            rotate.main(config, pat)
    assert events == (["push", "prune"] if pushed else ["push"])


if __name__ == "__main__":
    pytest_bazel.main()
