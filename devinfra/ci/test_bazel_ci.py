import json
import os
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

REPO_ROOT = Path(__file__).resolve().parents[2]
BAZEL_CI = REPO_ROOT / "devinfra/ci/bazel_ci.py"
RUNNER_PROBE = Path("devinfra/buildbuddy/ci/runner_probe.py")

type RunCI = Callable[[dict[str, str]], subprocess.CompletedProcess[str]]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def run_ci(tmp_path: Path) -> RunCI:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache_root = tmp_path / "xdg-cache"
    runfiles_root = Runfiles.CreateOrRaise().root()
    for command, asset in (("git", "fake_git.py"), ("bazel-diff", "fake_bazel_diff.py"), ("bazel", "fake_bazel.py")):
        _write_executable(bin_dir / command, (runfiles_root / f"ducktape/devinfra/ci/testing/{asset}").read_text())
    # Model the production Python invocation with a probe file in the checkout.
    # Tests that inspect diagnostics install the real script in this location.
    (bin_dir / "python3").symlink_to(sys.executable)
    probe = workspace / RUNNER_PROBE
    probe.parent.mkdir(parents=True)
    probe.touch()

    def run(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "GITHUB_EVENT_NAME": "push",
                "PR_HEAD_SHA": "",
                "PR_BASE_SHA": "",
                "CI_TEST_LOG_DIR": str(tmp_path),
                "XDG_CACHE_HOME": str(cache_root),
                "RBE_CONTAINER_IMAGE": "test-image",
                "TEST_INVOCATION_ID": "11111111-1111-1111-1111-111111111111",
                "BUILD_INVOCATION_ID": "22222222-2222-2222-2222-222222222222",
                "FAKE_TESTS": "//ci:normal_test",
                "FAKE_TEST_EXIT": "0",
                "FAKE_BUILD_EXIT": "0",
                "FAKE_GENERATE_LOCKFILE": "",
            }
        )
        env.update(overrides)
        return subprocess.run(
            [sys.executable, str(BAZEL_CI)], cwd=workspace, env=env, capture_output=True, text=True, check=False
        )

    return run


@pytest.fixture
def run_pr_with_probe(tmp_path: Path, run_ci: RunCI) -> RunCI:
    (tmp_path / "workspace" / RUNNER_PROBE).write_bytes((REPO_ROOT / RUNNER_PROBE).read_bytes())
    upload_digest = "a" * 64
    _write_executable(
        tmp_path / "bin" / "bb",
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "$FAKE_BB_UPLOAD_EXIT" != "0" ]; then echo "upload failed" >&2; exit "$FAKE_BB_UPLOAD_EXIT"; fi',
                'cp "$2" "$CI_TEST_LOG_DIR/probe-upload.tgz"',
                f'echo "uploaded /compressed-blobs/zstd/{upload_digest}/123"',
                "",
            ]
        ),
    )

    def run(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return run_ci(
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "PR_HEAD_SHA": "pr-head",
                "PR_BASE_SHA": "base",
                "CHANGED_FILE": "devinfra/ci/test_bazel_ci.py",
                "CI_VM_PROBE_DIR": str(tmp_path / "probe"),
                "FAKE_BB_UPLOAD_EXIT": "0",
            }
            | overrides
        )

    return run


@pytest.mark.parametrize(
    ("changed_file", "graph_wide"),
    [
        ("devinfra/ci/test_bazel_ci.py", False),
        ("MODULE.bazel", True),
        ("devinfra/bazel/oci.MODULE.bazel", True),
        ("devinfra/bazel/oci.MODULE.bazel.notes", False),
        ("third_party/cli_proxy_api/go.mod", True),
        ("third_party/activitywatch/package.json", True),
        ("pnpm-lock.yaml", True),
        ("Cargo.toml", True),
        ("pyproject.toml", True),
        ("devinfra/ci/bazel_ci.py", True),
        ("devinfra/ci/example.bzl", True),
    ],
)
def test_pr_target_selection(tmp_path: Path, run_ci: RunCI, changed_file: str, graph_wide: bool) -> None:
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
        assert bazel_diff_args[:3] == ["get-impacted-targets", "-w", str(tmp_path / "workspace")]
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
    assert "--@io_bazel_rules_go//go/config:race" not in build_args


@pytest.mark.parametrize(
    ("overrides", "expected", "phases"),
    [
        ({}, 0, ["test", "build"]),
        ({"FAKE_TESTS": ""}, 0, ["build"]),
        ({"FAKE_TEST_EXIT": "1"}, 1, ["test"]),
        ({"FAKE_TEST_EXIT": "3"}, 3, ["test", "build"]),
        ({"FAKE_TEST_EXIT": "3", "FAKE_BUILD_EXIT": "1"}, 3, ["test", "build"]),
        ({"GITHUB_EVENT_NAME": "workflow_dispatch", "FAKE_TEST_EXIT": "3"}, 3, ["test"]),
        (
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "PR_HEAD_SHA": "pr-head",
                "CHANGED_FILE": "a.py",
                "FAKE_TEST_EXIT": "3",
            },
            3,
            ["test"],
        ),
        ({"FAKE_TEST_EXIT": "4"}, 4, ["test"]),
        ({"FAKE_BUILD_EXIT": "1"}, 1, ["test", "build"]),
        (
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "PR_HEAD_SHA": "pr-head",
                "CHANGED_FILE": "devinfra/ci/test_bazel_ci.py",
                "FAKE_TEST_EXIT": "4",
            },
            0,
            ["test", "build"],
        ),
    ],
)
def test_phase_order_and_failure_handling(
    tmp_path: Path, run_ci: RunCI, overrides: dict[str, str], expected: int, phases: list[str]
) -> None:
    result = run_ci(overrides)
    assert result.returncode == expected, result.stderr + result.stdout
    assert (tmp_path / "phases.log").read_text().splitlines() == phases


def test_module_lockfile_cache_survives_workspace_cleanup(tmp_path: Path, run_ci: RunCI) -> None:
    """A generated resolution lockfile is restored for the next cleaned checkout."""
    lockfile_contents = '{"lockFileVersion": 1}\n'
    workspace_lockfile = tmp_path / "workspace" / "MODULE.bazel.lock"
    cache_lockfile = tmp_path / "xdg-cache" / "ducktape" / "bazel" / "MODULE.bazel.lock"

    first = run_ci({"FAKE_GENERATE_LOCKFILE": lockfile_contents})
    assert first.returncode == 0, first.stderr + first.stdout
    assert cache_lockfile.read_text() == lockfile_contents

    workspace_lockfile.unlink()
    second = run_ci({"FAKE_EXPECT_LOCKFILE": lockfile_contents})
    assert second.returncode == 0, second.stderr + second.stdout
    assert workspace_lockfile.read_text() == lockfile_contents


@pytest.mark.parametrize("cached_contents", [None, "not valid JSON"])
def test_module_lockfile_cache_missing_or_corrupt_is_best_effort(
    tmp_path: Path, run_ci: RunCI, cached_contents: str | None
) -> None:
    lockfile_contents = '{"lockFileVersion": 1}\n'
    cache_lockfile = tmp_path / "xdg-cache" / "ducktape" / "bazel" / "MODULE.bazel.lock"
    if cached_contents is not None:
        cache_lockfile.parent.mkdir(parents=True)
        cache_lockfile.write_text(cached_contents)

    result = run_ci({"FAKE_GENERATE_LOCKFILE": lockfile_contents})

    assert result.returncode == 0, result.stderr + result.stdout
    assert cache_lockfile.read_text() == lockfile_contents
    if cached_contents is not None:
        assert "Ignoring invalid JSON in cached Bazel module lockfile" in result.stderr


def test_module_lockfile_cache_preserves_preexisting_workspace_file(tmp_path: Path, run_ci: RunCI) -> None:
    workspace_contents = '{"lockFileVersion": 2}\n'
    cached_contents = '{"lockFileVersion": 1}\n'
    workspace_lockfile = tmp_path / "workspace" / "MODULE.bazel.lock"
    cache_lockfile = tmp_path / "xdg-cache" / "ducktape" / "bazel" / "MODULE.bazel.lock"
    workspace_lockfile.write_text(workspace_contents)
    cache_lockfile.parent.mkdir(parents=True)
    cache_lockfile.write_text(cached_contents)

    result = run_ci({"FAKE_EXPECT_LOCKFILE": workspace_contents})

    assert result.returncode == 0, result.stderr + result.stdout
    assert cache_lockfile.read_text() == workspace_contents


def test_module_lockfile_cache_saves_lockfile_after_test_failure(tmp_path: Path, run_ci: RunCI) -> None:
    lockfile_contents = '{"lockFileVersion": 1}\n'
    cache_lockfile = tmp_path / "xdg-cache" / "ducktape" / "bazel" / "MODULE.bazel.lock"

    result = run_ci({"FAKE_GENERATE_LOCKFILE": lockfile_contents, "FAKE_TEST_EXIT": "1"})

    assert result.returncode == 1, result.stderr + result.stdout
    assert cache_lockfile.read_text() == lockfile_contents
    assert (tmp_path / "phases.log").read_text().splitlines() == ["test"]


def test_module_lockfile_cache_failure_preserves_test_status(tmp_path: Path, run_ci: RunCI) -> None:
    cache_root = tmp_path / "cache-root-is-a-file"
    cache_root.write_text("not a directory")

    result = run_ci(
        {"XDG_CACHE_HOME": str(cache_root), "FAKE_GENERATE_LOCKFILE": '{"lockFileVersion": 1}\n', "FAKE_TEST_EXIT": "1"}
    )

    assert result.returncode == 1, result.stderr + result.stdout
    assert "Could not save Bazel module lockfile cache" in result.stderr
    assert (tmp_path / "phases.log").read_text().splitlines() == ["test"]


def test_selected_test_labels_are_archived_with_source_and_invocation_identity(
    tmp_path: Path, run_pr_with_probe: RunCI
) -> None:
    result = run_pr_with_probe(
        {"FAKE_TESTS": "//ci:first_test\n//ci:second_test", "BUILDBUDDY_API_KEY": "test-secret-not-for-snapshot"}
    )

    assert result.returncode == 0, result.stderr + result.stdout
    archive_path = tmp_path / "probe-upload.tgz"
    assert archive_path.is_file()
    with tarfile.open(archive_path) as archive:
        snapshot_file = archive.extractfile("selected-test-labels.json")
        assert snapshot_file is not None
        snapshot = json.load(snapshot_file)

    assert snapshot == {
        "schema": "ducktape.bazel_ci_selected_test_labels.v1",
        "selection": {
            "stage": "bazel-query-before-runtime-tag-filter",
            "full_sweep": False,
            "labels": ["//ci:first_test", "//ci:second_test"],
        },
        "source": {
            "commit_sha": "merge",
            "base_sha": "base",
            "merge_sha": "merge",
            "pr_head_sha": "pr-head",
            "event_base_sha": "base",
        },
        "workflow": {
            "name": "Bazel CI",
            "event_name": "pull_request",
            "test_invocation_id": "11111111-1111-1111-1111-111111111111",
            "build_invocation_id": "22222222-2222-2222-2222-222222222222",
        },
    }
    assert "test-secret-not-for-snapshot" not in json.dumps(snapshot)
    assert "CI_VM_PROBE_CAS digest=/compressed-blobs/zstd/" + "a" * 64 + "/123" in result.stdout
    assert "CI_VM_PROBE_CAS_DOWNLOAD=bb download /compressed-blobs/zstd/" + "a" * 64 + "/123" in result.stdout


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param("FAKE_BB_UPLOAD_EXIT", id="probe-upload"),
        pytest.param("FAKE_SELECTION_SNAPSHOT_WRITE_FAIL", id="snapshot-write"),
    ],
)
@pytest.mark.parametrize(("test_exit", "expected", "phases"), [("0", 0, ["test", "build"]), ("1", 1, ["test"])])
def test_diagnostic_failure_preserves_gate_verdict(
    tmp_path: Path, run_pr_with_probe: RunCI, fault: str, test_exit: str, expected: int, phases: list[str]
) -> None:
    result = run_pr_with_probe({fault: "1", "FAKE_TEST_EXIT": test_exit})

    assert result.returncode == expected, result.stderr + result.stdout
    assert (tmp_path / "phases.log").read_text().splitlines() == phases
    if fault == "FAKE_BB_UPLOAD_EXIT":
        assert "CI_VM_PROBE_CAS unavailable=" in result.stdout
    else:
        assert "Could not write selected-test label snapshot" in result.stderr
        with tarfile.open(tmp_path / "probe-upload.tgz") as archive:
            assert "selected-test-labels.json" not in archive.getnames()


if __name__ == "__main__":
    pytest_bazel.main()
