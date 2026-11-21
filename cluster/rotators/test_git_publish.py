import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from cluster.rotators.git_publish import push_outputs, sparse_checkout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_sparse_publish_stages_only_rotated_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "devel")
    for filename in (".sops.yaml", "rotated.yaml", "unchanged.yaml", "excluded.yaml"):
        (source / filename).write_text("old\n")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "seed")

    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    sparse_checkout(remote.as_uri(), [".sops.yaml", Path("rotated.yaml"), Path("unchanged.yaml")])

    assert not Path("excluded.yaml").exists()
    assert Path(".sops.yaml").read_text() == "old\n"
    Path("rotated.yaml").write_text("new\n")
    Path("unchanged.yaml").write_text("unpublished\n")
    identity = {"author_name": "Test rotator", "author_email": "rotator@example.test", "subject": "test token"}
    assert push_outputs([Path("rotated.yaml")], names=["fake-token"], **identity)
    assert not push_outputs([Path("rotated.yaml")], names=["fake-token"], **identity)

    assert _git(remote, "show", "devel:rotated.yaml") == "new"
    assert _git(remote, "show", "devel:unchanged.yaml") == "old"
    assert _git(remote, "log", "-1", "--format=%ae", "devel") == "rotator@example.test"
    assert _git(remote, "log", "-1", "--format=%s", "devel").endswith(": fake-token")


if __name__ == "__main__":
    pytest_bazel.main()
