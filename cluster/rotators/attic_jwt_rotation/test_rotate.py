import base64
import json
import textwrap
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
    ("pull", "valid_for", "mint_count"),
    [
        pytest.param(None, timedelta(days=365), 1, id="empty"),
        pytest.param(["test-cache"], timedelta(days=365), 0, id="fresh"),
        pytest.param(["stale-cache"], timedelta(days=365), 1, id="scope-drift"),
        pytest.param(["test-cache"], timedelta(hours=2), 1, id="near-expiry"),
    ],
)
def test_kubernetes_rotation_publishes_secret_version_without_git_output(
    monkeypatch: pytest.MonkeyPatch, pull: list[str] | None, valid_for: timedelta, mint_count: int
) -> None:
    token = _make_kubernetes_only_token()
    renewed = _kubernetes_jwt(timedelta(days=730))
    fake = _FakeKubernetesRun(renewed)
    if pull is not None:
        _seed_kubernetes_secret(fake, _kubernetes_jwt(valid_for), pull=pull, push=[])
    monkeypatch.setattr(rotate.subprocess, "run", fake)

    assert rotate_one(token, Config(tokens=[token])) is False

    assert fake.mint_count == mint_count
    assert fake.secret is not None
    if mint_count:
        assert base64.b64decode(fake.secret["data"]["token"]).decode() == renewed
    assert fake.workspace_values == [f"test-secret-uid/{1 + mint_count}"]
    assert all(renewed not in " ".join(call) for call in fake.calls)
    assert not any(call[0] == "sops" for call in fake.calls)


def _seed_kubernetes_secret(fake: _FakeKubernetesRun, jwt: str, *, pull: list[str], push: list[str]) -> None:
    assert fake.secret is not None
    payload = rotate._decode_jwt_payload(jwt)
    assert payload is not None
    expires = datetime.fromtimestamp(payload["exp"], UTC)
    fake.secret["data"]["token"] = base64.b64encode(jwt.encode()).decode()
    fake.secret["metadata"]["annotations"] = {
        rotate._SECRET_EXPIRY_ANNOTATION: rotate._iso_expiry(expires),
        rotate._SECRET_PULL_ANNOTATION: json.dumps(sorted(pull), separators=(",", ":")),
        rotate._SECRET_PUSH_ANNOTATION: json.dumps(sorted(push), separators=(",", ":")),
    }


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


@pytest.mark.parametrize("mint_count", [0, 1], ids=["already-fresh", "mint-on-first-attempt"])
def test_kubernetes_notification_retry_does_not_remint(monkeypatch: pytest.MonkeyPatch, mint_count: int) -> None:
    jwt = _kubernetes_jwt(timedelta(days=365))
    token = _make_kubernetes_only_token()
    fake = _FakeKubernetesRun(jwt)
    if not mint_count:
        _seed_kubernetes_secret(fake, jwt, pull=["test-cache"], push=[])
    fake.fail_workspace_patch = True
    monkeypatch.setattr(rotate.subprocess, "run", fake)

    with pytest.raises(RuntimeError) as failure:
        rotate_one(token, Config(tokens=[]))
    assert jwt not in str(failure.value)
    assert rotate_one(token, Config(tokens=[])) is False

    assert fake.mint_count == mint_count
    assert sum("workspaces.opentofu.upbound.io" in call for call in fake.calls) == 2
    assert fake.workspace_values == [f"test-secret-uid/{1 + mint_count}"]
    assert not any(call[0] == "sops" for call in fake.calls)


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
    _seed_kubernetes_secret(fake, jwt, pull=["test-cache"], push=[])
    assert fake.secret is not None
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


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A local bare git repo on `devel` with one commit, as the rotator's push target.

    Bare so commit_and_push can push into refs/heads/devel without git's
    receive.denyCurrentBranch refusing an update to a checked-out branch.
    """
    remote = tmp_path / "upstream.git"
    repo = pygit2.init_repository(str(remote), bare=True)
    blob = repo.create_blob(b"creation_rules: []\n")
    builder = repo.TreeBuilder()
    builder.insert(".sops.yaml", blob, pygit2.GIT_FILEMODE_BLOB)
    tree = builder.write()
    sig = pygit2.Signature("test", "test@example.com")
    repo.create_commit("refs/heads/devel", sig, sig, "init", tree, [])
    return remote


def _devel_oid(path: Path) -> str:
    return str(pygit2.Repository(str(path)).references["refs/heads/devel"].target)


def test_clone_repo_clones_devel_from_local_remote(upstream: Path, tmp_path: Path):
    config = Config(tokens=[], git_remote=str(upstream), git_clone_depth=None)
    work = tmp_path / "work"
    repo = clone_repo(config, "unused-for-local-transport", work)
    assert (work / ".sops.yaml").read_text() == "creation_rules: []\n"
    assert repo.head.shorthand == "devel"


def test_commit_and_push_commits_and_pushes_rotated_files(upstream: Path, tmp_path: Path):
    config = Config(
        tokens=[make_token(name="wyrm2", sops_file="secrets/hosts/wyrm2-attic.yaml")],
        git_remote=str(upstream),
        git_clone_depth=None,
    )
    work = tmp_path / "work"
    repo = clone_repo(config, "unused-for-local-transport", work)

    sops_file = work / "secrets/hosts/wyrm2-attic.yaml"
    sops_file.parent.mkdir(parents=True, exist_ok=True)
    contents = textwrap.dedent("""\
        attic_token: newjwt
        expires_unencrypted: "2030-01-01T00:00:00Z"
        """)
    sops_file.write_text(contents)

    commit_and_push(repo, config, rotated=["wyrm2"], token="unused-for-local-transport")

    # A fresh clone of upstream reflects the pushed commit.
    verify = tmp_path / "verify"
    pygit2.clone_repository(str(upstream), str(verify), checkout_branch="devel")
    assert (verify / "secrets/hosts/wyrm2-attic.yaml").read_text() == contents


def test_commit_and_push_skips_when_nothing_staged(upstream: Path, tmp_path: Path):
    config = Config(tokens=[make_token()], git_remote=str(upstream), git_clone_depth=None)
    work = tmp_path / "work"
    repo = clone_repo(config, "unused-for-local-transport", work)

    before = _devel_oid(upstream)
    commit_and_push(repo, config, rotated=[], token="unused-for-local-transport")
    assert _devel_oid(upstream) == before


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


class _ScriptedTransport(httpx.MockTransport):
    """httpx transport that records incoming requests and replays scripted responses."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, bytes]] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), request.content))
        return self._responses.pop(0)


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="http://attic:8080", transport=transport)


def test_ensure_cache_returns_public_key_when_cache_exists():
    transport = _ScriptedTransport([httpx.Response(200, json={"public_key": "main:abc="})])
    with _client(transport) as client:
        assert ensure_cache(client, "main") == "main:abc="
    (call,) = transport.calls
    assert call[:2] == ("GET", "http://attic:8080/_api/v1/cache-config/main")


@pytest.mark.parametrize(
    ("kwargs", "body_overrides"),
    [
        ({}, {}),
        ({"is_public": True}, {"is_public": True}),
        ({"keypair": "main:supplied-secret=="}, {"keypair": {"Keypair": "main:supplied-secret=="}}),
    ],
    ids=["private", "public", "supplied-keypair"],
)
def test_ensure_cache_creates_when_absent(kwargs: dict[str, Any], body_overrides: dict[str, Any]):
    transport = _ScriptedTransport(
        [
            httpx.Response(404, text="not found"),
            httpx.Response(201, text=""),
            httpx.Response(200, json={"public_key": "main:xyz="}),
        ]
    )
    with _client(transport) as client:
        assert ensure_cache(client, "main", **kwargs) == "main:xyz="
    assert [c[0] for c in transport.calls] == ["GET", "POST", "GET"]
    # Matches upstream attic's `attic cache create` default with no flags, plus the requested overrides.
    assert json.loads(transport.calls[1][2]) == {
        "keypair": "Generate",
        "is_public": False,
        "store_dir": "/nix/store",
        "priority": 41,
        "upstream_cache_key_names": [],
        **body_overrides,
    }


def test_load_keypair_returns_none_when_dir_is_none():
    assert _load_keypair(None, "main") is None


def test_load_keypair_returns_none_when_cache_file_missing(tmp_path: Path):
    assert _load_keypair(tmp_path, "main") is None


def test_load_keypair_reads_and_strips_file(tmp_path: Path):
    (tmp_path / "main").write_text("main:secret==\n")
    assert _load_keypair(tmp_path, "main") == "main:secret=="


def test_ensure_cache_raises_on_unexpected_error():
    transport = _ScriptedTransport([httpx.Response(500, text="boom")])
    with _client(transport) as client, pytest.raises(RuntimeError):
        ensure_cache(client, "main")


def test_ensure_cache_raises_when_create_fails():
    transport = _ScriptedTransport([httpx.Response(404, text=""), httpx.Response(403, text="denied")])
    with _client(transport) as client, pytest.raises(RuntimeError):
        ensure_cache(client, "main")


if __name__ == "__main__":
    pytest_bazel.main()
