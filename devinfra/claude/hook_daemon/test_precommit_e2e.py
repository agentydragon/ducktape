"""E2E tests for precommit_runner with real pre-commit hooks.

Sets up a temporary git repo with local pre-commit hooks and verifies that
run_on_file correctly identifies which hooks modified files vs just failed.

# Snapshot update workflow: see root AGENTS.md "Updating syrupy snapshots".
"""

import sys
from pathlib import Path
from textwrap import dedent

import pygit2
import pytest
import pytest_bazel
import yaml
from syrupy.assertion import SnapshotAssertion

from devinfra.claude.hook_daemon.post_tool_use import _format_check_result
from devinfra.claude.hook_daemon.precommit_runner import run_on_file

# Relative path used in _format_check_result to keep snapshots stable
# (avoids embedding tmp dir absolute paths).
_TEST_FILE = Path("test.py")


def _make_script(repo: Path, name: str, body: str) -> Path:
    script = repo / name
    script.write_text(f"#!{sys.executable}\n{dedent(body)}")
    script.chmod(0o755)
    return script


@pytest.fixture
def precommit_repo(tmp_path: Path) -> Path:
    """Git repo with three local hooks: fixer, checker, passthrough."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    repo = pygit2.init_repository(str(repo_path))
    repo.config["user.name"] = "Test"
    repo.config["user.email"] = "test@test.com"

    # Hook 1: replaces 'foo' with 'bar', exits 1 on change
    _make_script(
        repo_path,
        "fixer.py",
        """\
        import sys
        from pathlib import Path

        changed = False
        for f in sys.argv[1:]:
            p = Path(f)
            content = p.read_text()
            new = content.replace("foo", "bar")
            if new != content:
                p.write_text(new)
                changed = True
        sys.exit(1 if changed else 0)
        """,
    )

    # Hook 2: exits 1 if file contains 'BANNED', never modifies
    _make_script(
        repo_path,
        "checker.py",
        """\
        import sys
        from pathlib import Path

        for f in sys.argv[1:]:
            if "BANNED" in Path(f).read_text():
                print(f"{f}: contains BANNED keyword")
                sys.exit(1)
        sys.exit(0)
        """,
    )

    # Hook 3: always passes
    _make_script(
        repo_path,
        "passthrough.py",
        """\
        import sys
        sys.exit(0)
        """,
    )

    config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "fixer",
                        "name": "fixer (foo->bar)",
                        "entry": f"{sys.executable} {repo_path / 'fixer.py'}",
                        "language": "system",
                        "pass_filenames": True,
                    },
                    {
                        "id": "checker",
                        "name": "checker (no BANNED)",
                        "entry": f"{sys.executable} {repo_path / 'checker.py'}",
                        "language": "system",
                        "pass_filenames": True,
                    },
                    {
                        "id": "passthrough",
                        "name": "passthrough",
                        "entry": f"{sys.executable} {repo_path / 'passthrough.py'}",
                        "language": "system",
                        "pass_filenames": True,
                    },
                ],
            }
        ]
    }

    (repo_path / ".pre-commit-config.yaml").write_text(yaml.dump(config))

    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Test", "test@test.com")
    repo.create_commit("HEAD", sig, sig, "init", tree, [])

    return repo_path


def test_fixer_modifies_checker_fails(precommit_repo: Path, snapshot: SnapshotAssertion) -> None:
    """Fixer modifies file, checker fails without modifying — correct labels."""
    test_file = precommit_repo / "test.py"
    test_file.write_text("foo = 1  # BANNED\n")

    result = run_on_file(test_file, precommit_repo)

    hooks = {h.hook_id: h for h in result.hooks}
    assert hooks["fixer"].files_modified is True
    assert hooks["fixer"].passed is False
    assert hooks["checker"].files_modified is False
    assert hooks["checker"].passed is False
    assert hooks["passthrough"].passed is True

    output = _format_check_result(result, _TEST_FILE)
    assert output == snapshot


def test_only_checker_fails(precommit_repo: Path, snapshot: SnapshotAssertion) -> None:
    """No fixer trigger, only checker fails — single non-zero exit."""
    test_file = precommit_repo / "test.py"
    test_file.write_text("clean = 1  # BANNED\n")

    result = run_on_file(test_file, precommit_repo)

    hooks = {h.hook_id: h for h in result.hooks}
    assert hooks["fixer"].passed is True
    assert hooks["checker"].files_modified is False
    assert hooks["checker"].passed is False
    assert hooks["passthrough"].passed is True

    output = _format_check_result(result, _TEST_FILE)
    assert output == snapshot


def test_all_pass(precommit_repo: Path) -> None:
    """Clean file — all hooks pass."""
    test_file = precommit_repo / "test.py"
    test_file.write_text("clean = 1\n")

    result = run_on_file(test_file, precommit_repo)
    assert result.all_passed


def test_file_restored_after_run(precommit_repo: Path) -> None:
    """Original file content is restored after run_on_file returns."""
    test_file = precommit_repo / "test.py"
    original = "foo = 1\n"
    test_file.write_text(original)

    result = run_on_file(test_file, precommit_repo)

    # Fixer should have changed foo->bar, but file is restored
    assert not result.all_passed
    assert test_file.read_text() == original


if __name__ == "__main__":
    pytest_bazel.main()
