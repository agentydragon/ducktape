import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import certifi
import pygit2
import pytest
import pytest_bazel
import rotate
import yaml
from pydantic import ValidationError
from rotate import (
    Config,
    Token,
    _configure_ca_trust,
    clone_repo,
    commit_and_push,
    jwt_payload,
    mint_attic_token,
    remaining_hours,
    rotate_one,
)


def _make_jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


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


def test_jwt_payload_decodes_unpadded_base64url():
    claims = {"sub": "wyrm2", "exp": 1_800_000_000}
    assert jwt_payload(_make_jwt(claims)) == claims


def test_remaining_hours_missing_file_is_none(tmp_path: Path):
    assert remaining_hours(tmp_path / "absent.yaml") is None


def test_remaining_hours_unstamped_file_is_none(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text("attic_token: abc\n")
    assert remaining_hours(f) is None


def test_remaining_hours_reads_unencrypted_expiry(tmp_path: Path):
    expires = datetime.now(UTC) + timedelta(hours=10)
    f = tmp_path / "t.yaml"
    f.write_text(f'expires_unencrypted: "{expires:%Y-%m-%dT%H:%M:%SZ}"\nattic_token: abc\n')
    remaining = remaining_hours(f)
    assert remaining is not None
    assert 9 < remaining < 11


def test_mint_attic_token_builds_kubectl_exec_atticadm_argv(monkeypatch):
    token = Token(
        name="ducktape CI attic writer (main)",
        sops_file=Path("secrets/ci/attic-main-writer.sops.yaml"),
        sub="ducktape-ci",
        validity="1 year",
        pull=["main", "gaffer"],
        push=["main"],
    )
    fake = _FakeRun(jwt="signed.token")
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    assert mint_attic_token(token, Config(tokens=[])) == "signed.token"
    (call,) = fake.calls
    assert call == [
        "kubectl",
        "-n",
        "nix-cache",
        "exec",
        "deploy/attic",
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
    token = Token(name="x", sops_file=Path("s.yaml"), sub="x", validity="1 year", pull=[], push=[])
    monkeypatch.setattr(rotate.subprocess, "run", _FakeRun(jwt=""))
    with pytest.raises(RuntimeError, match="empty output"):
        mint_attic_token(token, Config(tokens=[]))


def test_rotate_one_skips_when_fresh(monkeypatch, tmp_path: Path):
    sops_file = tmp_path / "t.yaml"
    expires = datetime.now(UTC) + timedelta(hours=48)
    sops_file.write_text(f'expires_unencrypted: "{expires:%Y-%m-%dT%H:%M:%SZ}"\nattic_token: old\n')
    token = Token(name="x", sops_file=sops_file, sub="x", validity="1 year", pull=[], push=[])
    fake = _FakeRun(jwt="should-not-be-used")
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    assert rotate_one(token, Config(tokens=[])) is False
    assert not fake.calls  # no kubectl mint, no sops


def test_rotate_one_mints_and_writes_when_absent(monkeypatch, tmp_path: Path):
    sops_file = tmp_path / "sub" / "t.yaml"
    exp = int((datetime.now(UTC) + timedelta(days=365)).timestamp())
    jwt = _make_jwt({"sub": "x", "exp": exp})
    token = Token(name="x", sops_file=sops_file, sub="x", validity="1 year", pull=["main"], push=[])
    fake = _FakeRun(jwt=jwt)
    monkeypatch.setattr(rotate.subprocess, "run", fake)
    assert rotate_one(token, Config(tokens=[])) is True
    assert any("atticadm" in c for c in fake.calls)
    assert fake.calls[-1][0] == "sops"  # encrypt --in-place runs last
    written = yaml.safe_load(sops_file.read_text())
    assert written["attic_token"] == jwt
    assert "expires_unencrypted" in written


def test_token_requires_all_fields():
    with pytest.raises(ValidationError):
        Token.model_validate({"name": "x"})  # missing sops_file, sub, validity, pull


def test_token_push_defaults_to_empty():
    token = Token.model_validate(
        {"name": "x", "sops_file": "s.yaml", "sub": "x", "validity": "1 year", "pull": ["main"]}
    )
    assert token.push == []


def test_config_parses_tokens_and_defaults():
    config = Config.model_validate(
        {
            "tokens": [
                {
                    "name": "wyrm2 attic reader",
                    "sops_file": "secrets/hosts/wyrm2-attic.yaml",
                    "sub": "wyrm2",
                    "validity": "1 year",
                    "pull": ["main", "gaffer"],
                    "push": [],
                }
            ]
        }
    )
    (token,) = config.tokens
    assert token.sub == "wyrm2"
    assert token.pull == ["main", "gaffer"]
    assert config.attic_namespace == "nix-cache"
    assert config.attic_deployment == "deploy/attic"
    assert config.server_config == "/config/server.toml"
    assert config.token_field == "attic_token"
    assert config.rotate_below_hours == 24


# --- pygit2 git ops (clone_repo / commit_and_push) ---


def make_token(name: str = "x", sops_file: str = "s.yaml", **kw: Any) -> Token:
    base: dict[str, Any] = {"name": name, "sops_file": sops_file, "sub": name, "validity": "1 year", "pull": ["main"]}
    base.update(kw)
    return Token.model_validate(base)


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
    sops_file.write_text('attic_token: newjwt\nexpires_unencrypted: "2030-01-01T00:00:00Z"\n')

    commit_and_push(repo, config, rotated=["wyrm2"], token="unused-for-local-transport")

    # A fresh clone of upstream reflects the pushed commit.
    verify = tmp_path / "verify"
    pygit2.clone_repository(str(upstream), str(verify), checkout_branch="devel")
    assert (verify / "secrets/hosts/wyrm2-attic.yaml").read_text() == (
        'attic_token: newjwt\nexpires_unencrypted: "2030-01-01T00:00:00Z"\n'
    )
    assert "rotate attic JWTs" in pygit2.Repository(str(verify)).head.peel(pygit2.Commit).message


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


if __name__ == "__main__":
    pytest_bazel.main()
