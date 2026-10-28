import pytest_bazel

from devinfra.ci.invocation_ids import invocation_id


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
    assert invocation_id(run_id="33060467222", attempt="1", role="race") != base
    assert invocation_id(run_id="33060467222", attempt="1", role="race") != invocation_id(
        run_id="33060467222", attempt="1", role="build"
    )


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


if __name__ == "__main__":
    pytest_bazel.main()
