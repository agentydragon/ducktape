from pathlib import Path

import pytest

from cluster.rotators.git_publish import enter_sparse_checkout, push_outputs
from testing.git_repo import git, init_git_repo


def test_sparse_publish_stages_only_rotated_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_git_repo(source, branch="devel")
    for filename in (".sops.yaml", "rotated.yaml", "unchanged.yaml", "excluded.yaml"):
        (source / filename).write_text("old\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "seed")

    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    monkeypatch.chdir(tmp_path)
    enter_sparse_checkout(tmp_path / "checkout", remote.as_uri(), [Path("rotated.yaml"), Path("unchanged.yaml")])

    assert not Path("excluded.yaml").exists()
    assert Path(".sops.yaml").read_text() == "old\n"
    Path("rotated.yaml").write_text("new\n")
    Path("unchanged.yaml").write_text("unpublished\n")
    identity = {"author_name": "Test rotator", "author_email": "rotator@example.test", "subject": "test token"}
    assert push_outputs([Path("rotated.yaml")], names=["fake-token"], **identity)
    assert not push_outputs([Path("rotated.yaml")], names=["fake-token"], **identity)

    assert git(remote, "show", "devel:rotated.yaml") == "new\n"
    assert git(remote, "show", "devel:unchanged.yaml") == "old\n"
    assert git(remote, "log", "-1", "--format=%ae", "devel").strip() == "rotator@example.test"
    assert git(remote, "log", "-1", "--format=%s", "devel").strip().endswith(": fake-token")
