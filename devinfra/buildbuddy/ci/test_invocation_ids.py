from pathlib import Path

import pytest_bazel

from devinfra.buildbuddy.ci.invocation_ids import invocation_id, write_bazel_ci_outputs


def parse_github_outputs(contents: str) -> dict[str, str]:
    lines = iter(contents.splitlines())
    outputs: dict[str, str] = {}
    for line in lines:
        if "<<" not in line:
            key, value = line.split("=", 1)
            outputs[key] = value
            continue
        key, delimiter = line.split("<<", 1)
        value_lines: list[str] = []
        for value_line in lines:
            if value_line == delimiter:
                break
            value_lines.append(value_line)
        else:
            raise AssertionError(f"unterminated GitHub output {key!r}")
        outputs[key] = "\n".join(value_lines)
    return outputs


def test_the_derivation_is_a_function_of_the_run_alone() -> None:
    """Two processes that never meet — `bazel-ci.yml` naming the invocation and the
    pr-visuals publisher looking for it — must land on the same value from the same run."""
    assert invocation_id(run_id="33060467222", attempt="1", role="test") == invocation_id(
        run_id="33060467222", attempt="1", role="test"
    )
    assert str(invocation_id(run_id="33060467222", attempt="1", role="test")) == (
        "5eeb4c81-4316-5f84-ba47-3de435c5d2ad"
    )


def test_every_input_separates_the_result() -> None:
    """A re-run must not fold into the run it replaces, and the build invocation must not
    claim the test invocation's ID: BuildBuddy merges two invocations sharing an ID rather
    than rejecting the second, so a collision is silent."""
    base = invocation_id(run_id="33060467222", attempt="1", role="test")
    assert invocation_id(run_id="33060467223", attempt="1", role="test") != base
    assert invocation_id(run_id="33060467222", attempt="2", role="test") != base
    assert invocation_id(run_id="33060467222", attempt="1", role="build") != base


def test_scope_separates_parallel_release_artifact_builds() -> None:
    """Each package and build phase in the release matrix needs its own Bazel invocation."""
    unstamped = invocation_id(run_id="33060467222", attempt="1", role="release-unstamped", scope="gazelle")
    assert unstamped == invocation_id(run_id="33060467222", attempt="1", role="release-unstamped", scope="gazelle")
    assert invocation_id(run_id="33060467222", attempt="1", role="release-unstamped", scope="gterm-theme") != unstamped
    assert invocation_id(run_id="33060467222", attempt="1", role="release-stamped", scope="gazelle") != unstamped


def test_bazel_accepts_the_shape() -> None:
    """`--invocation_id` takes a UUID; anything else is rejected at the flag parser."""
    result = invocation_id(run_id="33060467222", attempt="1", role="test")
    assert result.version == 5
    assert len(str(result)) == 36


def test_bazel_ci_outputs_keep_allocated_ids_aligned_across_consumers(tmp_path: Path) -> None:
    github_output = tmp_path / "github-output"
    write_bazel_ci_outputs(github_output, run_id="33060467222", attempt="1")
    outputs = parse_github_outputs(github_output.read_text())
    role_ids = {role: outputs[role] for role in ("test", "build")}

    assert role_ids == {
        role: str(invocation_id(run_id="33060467222", attempt="1", role=role)) for role in ("test", "build")
    }
    assert outputs["linkage_roles"].split(",") == ["test", "build"]
    all_ids = outputs["all_invocation_ids"].split(",")
    assert all_ids == [role_ids[role] for role in ("test", "build")]
    assert list(zip(outputs["linkage_roles"].split(","), all_ids, strict=True)) == [
        ("test", role_ids["test"]),
        ("build", role_ids["build"]),
    ]
    assert outputs["publication"].split(",") == [role_ids["build"]]
    assert role_ids["test"] not in outputs["publication"].split(",")
    assert outputs["env_overrides"].splitlines() == [
        f"TEST_INVOCATION_ID={role_ids['test']}",
        f"BUILD_INVOCATION_ID={role_ids['build']}",
    ]


if __name__ == "__main__":
    pytest_bazel.main()
