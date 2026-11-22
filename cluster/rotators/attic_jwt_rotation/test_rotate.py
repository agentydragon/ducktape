import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import certifi
import httpx
import pygit2
import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from cluster.rotators import sops
from cluster.rotators.attic_jwt_rotation import rotate
from cluster.rotators.attic_jwt_rotation.config import Config, KubernetesDestination, SopsDestination, Token
from cluster.rotators.attic_jwt_rotation.rotate import (
    _configure_ca_trust,
    _load_keypair,
    clone_repo,
    commit_and_push,
    ensure_cache,
    mint_admin_jwt,
    mint_attic_token,
    rotate_one,
)


def _make_jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeRun:
    """Records subprocess.run calls; returns the jwt on atticadm mint, no-op otherwise."""

    def __init__(self, jwt: str = "the.jwt"):
        self.calls: list[list[str]] = []
        self._jwt = jwt

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        if "atticadm" in args:
            return _Completed(stdout=self._jwt + "\n")
        return _Completed()


class _FakeKubernetesRun:
    """Models the named Secret and Workspace calls made by one rotator invocation."""

    def __init__(self, jwt: str):
        self.jwt = jwt
        self.secret: dict[str, Any] | None = {
            "metadata": {
                "name": "test-attic-token",
                "namespace": "test-cache",
                "uid": "test-secret-uid",
                "resourceVersion": "1",
                "annotations": {},
            },
            "data": {},
        }
        self.calls: list[list[str]] = []
        self.workspace_values: list[str] = []
        self.fail_secret_patch = False
        self.fail_workspace_patch = False
        self.mint_count = 0

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if "atticadm" in args:
            self.mint_count += 1
            return _Completed(stdout=self.jwt + "\n")
        if "get" in args and "secret" in args:
            return _Completed(stdout=json.dumps(self.secret) if self.secret is not None else "")
        if "patch" in args and "secret" in args:
            if self.fail_secret_patch:
                self.fail_secret_patch = False
                return _Completed(stderr=f"fake API diagnostic contains {self.jwt}", returncode=1)
            assert self.secret is not None
            patch = json.loads(kwargs["input"])
            self.secret["data"].update(patch["data"])
            self.secret["metadata"]["annotations"].update(patch["metadata"]["annotations"])
            self.secret["metadata"]["resourceVersion"] = str(int(self.secret["metadata"]["resourceVersion"]) + 1)
            return _Completed(stdout="secret updated")
        if "workspaces.opentofu.upbound.io" in args:
            if self.fail_workspace_patch:
                self.fail_workspace_patch = False
                return _Completed(stderr=f"fake API diagnostic contains {self.jwt}", returncode=1)
            patch = json.loads(kwargs["input"])
            self.workspace_values.extend(patch["metadata"]["annotations"].values())
            return _Completed(stdout="workspace patched")
        return _Completed()


def _kubernetes_publication() -> dict[str, Any]:
    return {
        "secret": {"namespace": "test-cache", "name": "test-attic-token", "key": "token"},
        "notification": {"workspace": "test-github-secrets-sync", "annotation_key": "test.example/token-version"},
    }


def _make_kubernetes_only_token(**kwargs: Any) -> Token:
    fields: dict[str, Any] = {
        "name": "test Attic token",
        "sub": "test-subject",
        "validity": "1 year",
        "pull": ["test-cache"],
        "push": [],
        "destination": {"storage": "kubernetes", **_kubernetes_publication()},
    }
    fields.update(kwargs)
    token = Token.model_validate(fields)
    assert isinstance(token.destination, KubernetesDestination)
    return token


@pytest.mark.parametrize(
    "destination",
    [
        {"storage": "sops"},
        {"storage": "sops", "sops_file": "x.sops.yaml", **_kubernetes_publication()},
        {"storage": "kubernetes"},
        {"storage": "kubernetes", "sops_file": "x.sops.yaml", **_kubernetes_publication()},
        {"storage": "unknown", "sops_file": "x.sops.yaml"},
    ],
)
def test_config_rejects_missing_or_wrong_destination_fields(destination: dict[str, Any]):
    with pytest.raises(ValidationError):
        Config.model_validate(
            {"tokens": [{"name": "x", "sub": "x", "validity": "1 year", "pull": [], "destination": destination}]}
        )


def _kubernetes_jwt(valid_for: timedelta) -> str:
    return _make_jwt({"sub": "test-subject", "exp": int((datetime.now(UTC) + valid_for).timestamp())})


@pytest.mark.parametrize(
    ("pull", "push", "valid_for", "rotates"),
    [
        pytest.param(["test-cache"], [], timedelta(days=365), False, id="fresh"),
        pytest.param(["old-cache"], [], timedelta(days=365), True, id="pull-drift"),
        pytest.param(["test-cache"], ["old-cache"], timedelta(days=365), True, id="push-drift"),
        pytest.param(["test-cache"], [], timedelta(hours=2), True, id="near-expiry"),
    ],
)
def test_kubernetes_rotation_reconciles_the_previously_published_secret(
    monkeypatch: pytest.MonkeyPatch, pull: list[str], push: list[str], valid_for: timedelta, rotates: bool
) -> None:
    first_jwt = _kubernetes_jwt(valid_for)
    fake = _FakeKubernetesRun(first_jwt)
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    previous = _make_kubernetes_only_token(pull=pull, push=push)
    assert rotate_one(previous, Config(tokens=[previous], rotate_below_hours=1)) is False

    token = _make_kubernetes_only_token()
    fake.jwt = _kubernetes_jwt(timedelta(days=730))
    assert rotate_one(token, Config(tokens=[token])) is False

    assert fake.mint_count == (2 if rotates else 1)
    assert fake.secret is not None
    assert base64.b64decode(fake.secret["data"]["token"]).decode() == (fake.jwt if rotates else first_jwt)
    assert fake.workspace_values == ["test-secret-uid/2", "test-secret-uid/3" if rotates else "test-secret-uid/2"]
    assert all(jwt not in " ".join(call) for call in fake.calls for jwt in (first_jwt, fake.jwt))
    assert not any(call[0] == "sops" for call in fake.calls)


def test_mint_attic_token_builds_kubectl_exec_atticadm_argv(monkeypatch):
    token = Token(
        name="ducktape CI attic writer (main)",
        sub="ducktape-ci",
        validity="1 year",
        pull=["main", "gaffer"],
        push=["main"],
        destination=SopsDestination(sops_file=Path("secrets/ci/attic-main-writer.sops.yaml")),
    )
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    jwt = _make_jwt({"sub": token.sub, "exp": int(expires.timestamp())})
    fake = _FakeRun(jwt=jwt)
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    assert mint_attic_token(token, Config(tokens=[])) == (jwt, expires)
    (call,) = fake.calls
    assert call == [
        "kubectl",
        "-n",
        "nix-cache",
        "exec",
        "deploy/attic-next",
        "--",
        "atticadm",
        "-f",
        "/config/server.toml",
        "make-token",
        "--sub",
        "ducktape-ci",
        "--validity",
        "1 year",
        "--pull",
        "main",
        "--pull",
        "gaffer",
        "--push",
        "main",
    ]


def test_mint_attic_token_empty_output_raises(monkeypatch):
    token = Token(
        name="x", sub="x", validity="1 year", pull=[], push=[], destination=SopsDestination(sops_file=Path("s.yaml"))
    )
    monkeypatch.setattr(rotate.subprocess, "run", _FakeRun(jwt=""))
    with pytest.raises(RuntimeError):
        mint_attic_token(token, Config(tokens=[]))


@pytest.mark.parametrize(
    ("stamped_scope", "should_rotate"),
    [
        pytest.param(
            {"pull_unencrypted": ["gaffer", "main"], "push_unencrypted": ["main", "public"]}, False, id="match"
        ),
        pytest.param({"pull_unencrypted": ["gaffer", "main"], "push_unencrypted": ["main"]}, True, id="drift"),
        pytest.param({}, True, id="missing"),
    ],
)
def test_fresh_sops_rotation_requires_matching_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stamped_scope: dict[str, list[str]], should_rotate: bool
) -> None:
    # #3001: a previous token may remain valid for months after scope changes.
    sops_file = tmp_path / "token.yaml"
    expires = datetime.now(UTC) + timedelta(days=300)
    sops_file.write_text(
        yaml.safe_dump({"expires_unencrypted": f"{expires:%Y-%m-%dT%H:%M:%SZ}", "attic_token": "old", **stamped_scope})
    )
    token = Token(
        name="x",
        sub="x",
        validity="1 year",
        pull=["main", "gaffer"],
        push=["main", "public"],
        destination=SopsDestination(sops_file=sops_file),
    )
    fake = _FakeRun(jwt=_make_jwt({"sub": "x", "exp": int((expires + timedelta(days=65)).timestamp())}))
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    monkeypatch.setattr(sops, "prettier_format_yaml_in_place", lambda _: None)

    assert rotate_one(token, Config(tokens=[])) is should_rotate

    if should_rotate:
        assert yaml.safe_load(sops_file.read_text())["push_unencrypted"] == ["main", "public"]
    else:
        assert not fake.calls


def test_rotate_one_mints_and_writes_when_absent(monkeypatch, tmp_path: Path):
    sops_file = tmp_path / "sub" / "t.yaml"
    jwt = _make_jwt({"sub": "x", "exp": 1_800_000_000})
    token = Token(
        name="x", sub="x", validity="1 year", pull=["main"], push=[], destination=SopsDestination(sops_file=sops_file)
    )
    fake = _FakeRun(jwt=jwt)
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    monkeypatch.setattr(sops, "prettier_format_yaml_in_place", lambda _: None)
    assert rotate_one(token, Config(tokens=[])) is True
    assert any("atticadm" in c for c in fake.calls)
    written = yaml.safe_load(sops_file.read_text())
    assert written["attic_token"] == jwt
    assert written["expires_unencrypted"] == "2027-01-15T08:00:00Z"
    assert written["pull_unencrypted"] == ["main"]
    assert written["push_unencrypted"] == []


def test_wrong_subject_preserves_existing_sops_file(monkeypatch, tmp_path: Path):
    path = tmp_path / "token.yaml"
    existing = "attic_token: prior-ciphertext\n"
    path.write_text(existing)
    token = Token(
        name="reader", sub="reader", validity="1 year", pull=["main"], destination=SopsDestination(sops_file=path)
    )
    monkeypatch.setattr(rotate.subprocess, "run", _FakeRun(jwt=_make_jwt({"sub": "other", "exp": 1_800_000_000})))
    with pytest.raises(RuntimeError, match="subject differs"):
        rotate_one(token, Config(tokens=[token]))
    assert path.read_text() == existing


def test_kubernetes_destination_requires_the_precreated_secret(monkeypatch):
    token = _make_kubernetes_only_token()
    jwt = _kubernetes_jwt(timedelta(days=365))
    fake = _FakeKubernetesRun(jwt)
    fake.secret = None
    monkeypatch.setattr(rotate.subprocess, "run", fake)

    with pytest.raises(RuntimeError):
        rotate_one(token, Config(tokens=[]))

    assert fake.mint_count == 0
    assert len(fake.calls) == 1


def test_kubernetes_notification_retry_does_not_remint(monkeypatch: pytest.MonkeyPatch) -> None:
    jwt = _kubernetes_jwt(timedelta(days=365))
    token = _make_kubernetes_only_token()
    fake = _FakeKubernetesRun(jwt)
    monkeypatch.setattr(rotate.subprocess, "run", fake)

    # The first notification fails after publishing. The second starts with that fresh Secret.
    for _ in range(2):
        fake.fail_workspace_patch = True
        with pytest.raises(RuntimeError, match="patch Workspace") as failure:
            rotate_one(token, Config(tokens=[]))
        assert jwt not in str(failure.value)
    assert rotate_one(token, Config(tokens=[])) is False

    assert fake.mint_count == 1
    assert fake.workspace_values == ["test-secret-uid/2"]


def test_failed_kubernetes_secret_patch_is_sanitized_and_retried(monkeypatch):
    jwt = _kubernetes_jwt(timedelta(days=365))
    token = _make_kubernetes_only_token()
    fake = _FakeKubernetesRun(jwt)
    fake.fail_secret_patch = True
    monkeypatch.setattr(rotate.subprocess, "run", fake)

    with pytest.raises(RuntimeError) as failure:
        rotate_one(token, Config(tokens=[]))

    assert jwt not in str(failure.value)
    assert rotate_one(token, Config(tokens=[])) is False
    assert fake.mint_count == 2
    assert all(jwt not in " ".join(call) for call in fake.calls)


def test_kubernetes_secret_response_requires_uid_and_resource_version(monkeypatch):
    jwt = _kubernetes_jwt(timedelta(days=365))
    token = _make_kubernetes_only_token()
    fake = _FakeKubernetesRun(jwt)
    assert fake.secret is not None
    fake.secret["data"]["token"] = base64.b64encode(jwt.encode()).decode()
    del fake.secret["metadata"]["resourceVersion"]
    monkeypatch.setattr(rotate.subprocess, "run", fake)

    with pytest.raises(RuntimeError, match="invalid object or missing metadata") as failure:
        rotate_one(token, Config(tokens=[]))

    assert jwt not in str(failure.value)


def make_token(name: str = "x", sops_file: str = "s.yaml", **kw: Any) -> Token:
    base: dict[str, Any] = {
        "name": name,
        "sub": name,
        "validity": "1 year",
        "pull": ["main"],
        "destination": {"storage": "sops", "sops_file": sops_file},
    }
    base.update(kw)
    return Token.model_validate(base)


def test_token_without_push_grants_no_push_scope():
    # The generated roster omits `push` for the read-only tokens, so this default is their only source.
    assert make_token().push == []


def test_git_publication_pushes_only_rotated_files_and_skips_an_unchanged_retry(tmp_path: Path) -> None:
    remote = tmp_path / "upstream.git"
    upstream = pygit2.init_repository(str(remote), bare=True)
    builder = upstream.TreeBuilder()
    sops_config = b"creation_rules: []\n"
    builder.insert(".sops.yaml", upstream.create_blob(sops_config), pygit2.GIT_FILEMODE_BLOB)
    author = pygit2.Signature("test", "test@example.com")
    branch = "refs/heads/devel"
    initial = upstream.create_commit(branch, author, author, "init", builder.write(), [])

    config = Config(
        tokens=[
            make_token(name="workstation", sops_file="rotated.yaml"),
            make_token(name="other", sops_file="other.yaml"),
        ],
        git_remote=str(remote),
        git_clone_depth=None,
    )
    work = tmp_path / "work"
    repo = clone_repo(config, "unused-for-local-transport", work)
    assert repo.head.shorthand == "devel"
    assert (work / ".sops.yaml").read_bytes() == sops_config
    (work / "rotated.yaml").write_bytes(b"rotated output\n")
    (work / "other.yaml").write_bytes(b"unrelated change\n")

    commit_and_push(repo, config, rotated=["workstation"], token="unused-for-local-transport")
    published = upstream.revparse_single(branch).peel(pygit2.Commit)
    assert published.parent_ids == [initial]
    assert {entry.name for entry in published.tree} == {".sops.yaml", "rotated.yaml"}
    assert published.tree["rotated.yaml"].peel(pygit2.Blob).data == b"rotated output\n"

    commit_and_push(repo, config, rotated=["workstation"], token="unused-for-local-transport")
    assert upstream.references[branch].target == published.id


def test_configure_ca_trust_loads_a_real_bundle():
    # Regression: the libgit2 dir arg must be None, not "" (OpenSSL rejects the
    # empty string as "invalid directory" while loading the bundle).
    _configure_ca_trust(Path(certifi.where()))  # must not raise


def test_mint_admin_jwt_builds_kubectl_exec_atticadm_argv(monkeypatch):
    fake = _FakeRun(jwt="admin.jwt")
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    assert mint_admin_jwt("nix-cache", "deploy/attic-next", "/config/server.toml") == "admin.jwt"
    (call,) = fake.calls
    assert call == [
        "kubectl",
        "-n",
        "nix-cache",
        "exec",
        "deploy/attic-next",
        "--",
        "atticadm",
        "-f",
        "/config/server.toml",
        "make-token",
        "--sub",
        "bootstrap-admin",
        "--validity",
        "5 minutes",
        "--pull",
        "*",
        "--push",
        "*",
        "--create-cache",
        "*",
    ]


@pytest.mark.parametrize(
    ("kwargs", "body_overrides"),
    [
        ({}, {}),
        ({"is_public": True}, {"is_public": True}),
        ({"keypair": "main:supplied-secret=="}, {"keypair": {"Keypair": "main:supplied-secret=="}}),
    ],
    ids=["private", "public", "supplied-keypair"],
)
def test_ensure_cache_creates_once_and_returns_the_public_key(kwargs: dict[str, Any], body_overrides: dict[str, Any]):
    created = False

    def cache_api(request: httpx.Request) -> httpx.Response:
        nonlocal created
        assert request.url.path == "/_api/v1/cache-config/main"
        if request.method == "GET":
            return httpx.Response(200, json={"public_key": "main:xyz="}) if created else httpx.Response(404)
        assert request.method == "POST"
        assert not created
        assert json.loads(request.content) == {
            "keypair": "Generate",
            "is_public": False,
            "store_dir": "/nix/store",
            "priority": 41,
            "upstream_cache_key_names": [],
            **body_overrides,
        }
        created = True
        return httpx.Response(201)

    with httpx.Client(base_url="http://attic:8080", transport=httpx.MockTransport(cache_api)) as client:
        assert ensure_cache(client, "main", **kwargs) == "main:xyz="
        assert ensure_cache(client, "main", **kwargs) == "main:xyz="


def test_load_keypair_returns_none_when_dir_is_none():
    assert _load_keypair(None, "main") is None


def test_load_keypair_returns_none_when_cache_file_missing(tmp_path: Path):
    assert _load_keypair(tmp_path, "main") is None


def test_load_keypair_reads_and_strips_file(tmp_path: Path):
    (tmp_path / "main").write_text("main:secret==\n")
    assert _load_keypair(tmp_path, "main") == "main:secret=="


@pytest.mark.parametrize("statuses", [(500,), (404, 403)], ids=["lookup-failed", "creation-denied"])
def test_ensure_cache_reports_http_failure(statuses: tuple[int, ...]):
    responses = iter(statuses)
    transport = httpx.MockTransport(lambda _: httpx.Response(next(responses)))
    with (
        httpx.Client(base_url="http://attic:8080", transport=transport) as client,
        pytest.raises(RuntimeError, match=f"HTTP {statuses[-1]}"),
    ):
        ensure_cache(client, "main")


if __name__ == "__main__":
    pytest_bazel.main()
