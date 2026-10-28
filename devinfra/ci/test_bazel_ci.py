import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path

REPO_ROOT = Path(__file__).resolve().parents[2]
BAZEL_CI = REPO_ROOT / "devinfra/ci/bazel_ci.py"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def run_ci(tmp_path: Path) -> Callable[[dict[str, str]], subprocess.CompletedProcess[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command, asset in (("git", "fake_git.py"), ("bazel-diff", "fake_bazel_diff.py"), ("bazel", "fake_bazel.py")):
        _write_executable(bin_dir / command, get_required_path(f"_main/devinfra/ci/testing/{asset}").read_text())
    _write_executable(bin_dir / "python3", "#!/bin/sh\nexit 0\n")

    def run(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "GITHUB_EVENT_NAME": "push",
                "PR_HEAD_SHA": "",
                "PR_BASE_SHA": "",
                "CI_TEST_LOG_DIR": str(tmp_path),
                "RBE_CONTAINER_IMAGE": "test-image",
                "TEST_INVOCATION_ID": "11111111-1111-1111-1111-111111111111",
                "BUILD_INVOCATION_ID": "22222222-2222-2222-2222-222222222222",
                "RACE_INVOCATION_ID": "33333333-3333-3333-3333-333333333333",
                "FAKE_GO_TESTS": "//ci:go_test",
                "FAKE_TESTS": "//ci:normal_test",
                "FAKE_TEST_EXIT": "0",
                "FAKE_BUILD_EXIT": "0",
                "FAKE_RACE_EXIT": "0",
            }
        )
        env.update(overrides)
        return subprocess.run(
            [sys.executable, str(BAZEL_CI)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
        )

    return run


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
def test_pr_target_selection(
    tmp_path: Path,
    run_ci: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
    changed_file: str,
    graph_wide: bool,
) -> None:
    """Graph metadata uses //..., while ordinary changes retain bazel-diff filtering."""
    bazel_diff_args_log = tmp_path / "bazel-diff-args.log"
    affected_targets_log = tmp_path / "affected-targets.log"
    query_log = tmp_path / "query.log"
    test_args_log = tmp_path / "test-args.log"

    result = run_ci(
        {
            "GITHUB_EVENT_NAME": "pull_request",
            "PR_HEAD_SHA": "pr-head",
            "PR_BASE_SHA": "base",
            "CHANGED_FILE": changed_file,
        }
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
    build_args = (tmp_path / "build-args.log").read_text().splitlines()
    assert "--invocation_id=22222222-2222-2222-2222-222222222222" in build_args
    assert "--@io_bazel_rules_go//go/config:race" not in test_args
    assert "--@io_bazel_rules_go//go/config:race" not in build_args
    assert (tmp_path / "race-targets.log").read_text() == "//ci:go_test\n"
    race_args = (tmp_path / "race-args.log").read_text().splitlines()
    assert "--invocation_id=33333333-3333-3333-3333-333333333333" in race_args
    assert "--@io_bazel_rules_go//go/config:race" in race_args
    assert "--build_metadata=DISABLE_TARGET_TRACKING=true" in race_args
    go_test_query = (tmp_path / "go-test-query.log").read_text()
    assert go_test_query.startswith('let candidates = kind("go_test rule",')
    assert 'except attr("tags", "no_go_race", $candidates)' in go_test_query
    assert "manual|live_openai_api" not in go_test_query


@pytest.mark.parametrize(
    ("overrides", "expected", "phases"),
    [
        ({}, 0, ["test", "build", "race"]),
        ({"FAKE_TESTS": ""}, 0, ["build"]),
        ({"FAKE_GO_TESTS": ""}, 0, ["test", "build"]),
        ({"FAKE_TEST_EXIT": "1"}, 1, ["test"]),
        ({"FAKE_TEST_EXIT": "4"}, 4, ["test"]),
        ({"FAKE_BUILD_EXIT": "1"}, 1, ["test", "build"]),
        ({"FAKE_RACE_EXIT": "1"}, 1, ["test", "build", "race"]),
        ({"FAKE_RACE_EXIT": "4"}, 0, ["test", "build", "race"]),
        (
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "PR_HEAD_SHA": "pr-head",
                "CHANGED_FILE": "devinfra/ci/test_bazel_ci.py",
                "FAKE_TEST_EXIT": "4",
            },
            0,
            ["test", "build", "race"],
        ),
    ],
)
def test_phase_order_and_failure_handling(
    tmp_path: Path,
    run_ci: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
    overrides: dict[str, str],
    expected: int,
    phases: list[str],
) -> None:
    result = run_ci(overrides)
    assert result.returncode == expected, result.stderr + result.stdout
    assert (tmp_path / "phases.log").read_text().splitlines() == phases


if __name__ == "__main__":
    pytest_bazel.main()
