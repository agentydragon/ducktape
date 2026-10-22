import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path

REPO_ROOT = Path(__file__).resolve().parents[2]
BAZEL_CI = REPO_ROOT / "devinfra/ci/bazel_ci.py"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("changed_file", "graph_wide"),
    [
        ("devinfra/ci/test_bazel_ci.py", False),
        ("MODULE.bazel", True),
        ("third_party/cli_proxy_api/go.mod", True),
        ("third_party/activitywatch/package.json", True),
        ("pnpm-lock.yaml", True),
        ("Cargo.toml", True),
        ("pyproject.toml", True),
        ("devinfra/ci/bazel_ci.py", True),
        ("devinfra/ci/example.bzl", True),
    ],
)
def test_pr_target_selection(tmp_path: Path, changed_file: str, graph_wide: bool) -> None:
    """Graph metadata uses //..., while ordinary changes retain bazel-diff filtering."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bazel_diff_args_log = tmp_path / "bazel-diff-args.log"
    affected_targets_log = tmp_path / "affected-targets.log"
    query_log = tmp_path / "query.log"
    test_args_log = tmp_path / "test-args.log"

    for command, asset in (("git", "fake_git.py"), ("bazel-diff", "fake_bazel_diff.py"), ("bazel", "fake_bazel.py")):
        _write_executable(bin_dir / command, get_required_path(f"_main/devinfra/ci/testing/{asset}").read_text())
    _write_executable(bin_dir / "python3", "#!/bin/sh\nexit 0\n")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GITHUB_EVENT_NAME": "pull_request",
            "PR_HEAD_SHA": "pr-head",
            "PR_BASE_SHA": "base",
            "CHANGED_FILE": changed_file,
            "CI_TEST_LOG_DIR": str(tmp_path),
            "RBE_CONTAINER_IMAGE": "test-image",
            "TEST_INVOCATION_ID": "11111111-1111-1111-1111-111111111111",
            "BUILD_INVOCATION_ID": "22222222-2222-2222-2222-222222222222",
        }
    )
    result = subprocess.run(
        [sys.executable, str(BAZEL_CI)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr + result.stdout
    test_args = test_args_log.read_text().splitlines()
    if graph_wide:
        assert not bazel_diff_args_log.exists()
        assert "//..." in test_args
        assert not any(arg.startswith("--target_pattern_file=") for arg in test_args)
    else:
        assert affected_targets_log.read_text() == (
            "//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir\n//ci:normal_test\n"
        )
        bazel_diff_args = bazel_diff_args_log.read_text().splitlines()
        assert bazel_diff_args[:3] == ["get-impacted-targets", "-w", str(REPO_ROOT)]
        assert any(arg.startswith("--target_pattern_file=") for arg in test_args)
        query = query_log.read_text()
        assert 'except kind("source file", set(' in query
        assert 'except attr("tags", "manual", set(' in query
        assert '"//:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir"' in query

    # The pre-assigned ID has to reach Bazel: it is the only handle a consumer has on
    # this invocation when the run is cancelled before `bb remote` returns.
    assert "--invocation_id=11111111-1111-1111-1111-111111111111" in test_args


if __name__ == "__main__":
    pytest_bazel.main()
