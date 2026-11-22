import base64
import json
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_bazel
import yaml

from cluster.rotators.forgejo_token_rotation import rotate
from cluster.rotators.forgejo_token_rotation.config import FULL_ACCOUNT_SCOPES, Config, Rotation, TeaSecretOutput
from cluster.rotators.forgejo_token_rotation.rotate import (
    ForgejoCredentials,
    RotatedToken,
    mint_token,
    should_rotate,
    tokens_to_prune,
    write_outputs,
)


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


def test_minted_account_token_is_published_for_raw_and_tea_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    out = TeaSecretOutput(
        path=Path("tea.yaml"),
        name="forgejo-token",
        namespace="target",
        annotations={"reflector.v1.k8s.emberstack.com/reflection-allowed": "true"},
    )
    rotation = Rotation(
        name="haku",
        token_prefix="forgejo-tea-haku",
        credentials_dir=Path("/creds"),
        sops_file=Path("raw.yaml"),
        tea_secret=out,
    )
    creds = ForgejoCredentials("test-user", "test-secret", "http://forgejo.test:3000", "https://git.test")
    expected_name = "forgejo-tea-haku-20260701010203456789"

    def forgejo(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "http://forgejo.test:3000/api/v1/users/test-user/tokens"
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"test-user:test-secret").decode()
        # Omitting repositories selects the full account. Keep the reviewed scopes independent of production defaults.
        assert json.loads(request.content) == {
            "name": expected_name,
            "scopes": [
                "write:activitypub",
                "write:issue",
                "write:misc",
                "write:notification",
                "write:organization",
                "write:package",
                "write:repository",
                "write:user",
            ],
        }
        return httpx.Response(201, json={"id": 12, "name": expected_name, "token": "token-value"})

    outputs: dict[Path, dict[str, Any]] = {}

    def capture(path: Path, document: dict[str, Any]) -> None:
        outputs[path] = document

    monkeypatch.setattr(rotate, "write_sops_yaml", capture)
    now = datetime(2026, 7, 1, 1, 2, 3, 456789, tzinfo=UTC)
    with httpx.Client(transport=httpx.MockTransport(forgejo)) as client:
        write_outputs(rotation, creds, mint_token(client, rotation, creds, now), now)

    raw = outputs[rotation.sops_file]
    listed = {"id": 12, "name": expected_name, "token_last_eight": "en-value"}
    due, _ = should_rotate(rotation, raw, [listed], now)
    assert not due
    manifest = outputs[out.path]
    metadata = manifest["metadata"]
    assert (metadata["namespace"], metadata["name"]) == (out.namespace, out.name)
    assert metadata["annotations"].items() >= out.annotations.items()
    assert metadata["annotations"]["description"]
    data = manifest["stringData"]
    assert data["token-last-eight"] == raw["token_last_eight_unencrypted"] == listed["token_last_eight"]
    [login] = yaml.safe_load(data["config.yml"])["logins"]
    assert login["url"] == data["url"] == raw["url"] == creds.tea_url
    assert login["token"] == data["token"] == raw["token"] == "token-value"
    assert login["user"] == data["username"] == raw["username"] == creds.username
    assert login["name"] == "forgejo"
    assert login["default"] is True
    assert login["version_check"] is False


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
