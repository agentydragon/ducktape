import pytest
import pytest_bazel

from devinfra.ci.bes import Invocation, TestSummary
from devinfra.ci.invocation_ids import invocation_id
from devinfra.ci.publication_test_verdicts import recorded_test_failures, selector_already_failed

RUN_ID = "4077673030700"
ATTEMPT = "1"
SOURCE = "e7696450d7940000000000000000000000000000"
RBE_IMAGE = "ghcr.io/example/rbe@sha256:0000000000000000000000000000000000000000000000000000000000000000"
TEST_INVOCATION_ID = str(invocation_id(run_id=RUN_ID, attempt=ATTEMPT, role="test"))


def summary(label: str, status: str) -> TestSummary:
    return TestSummary(label=label, configuration_id="cfg", status=status, attempt_count=1, cached_count=None)


def push_sweep(summaries: list[TestSummary], *, finished: bool = True) -> Invocation:
    """bazel-ci's own `//...` test invocation of SOURCE, as the planners read it back."""
    return Invocation(
        outputs=[],
        test_status={s.label: s.status for s in summaries},
        test_summaries=summaries,
        finished=finished,
        successful=all(s.status == "PASSED" for s in summaries),
        command="test",
        requested_patterns=("//...",),
        explicit_cmd_line=(
            "--keep_going",
            "--config=rbe",
            "--config=ci",
            "--build_metadata=ROLE=CI",
            "--build_metadata=DISABLE_TARGET_TRACKING=false",
            f"--remote_default_exec_properties=container-image=docker://{RBE_IMAGE}",
            f"--invocation_id={TEST_INVOCATION_ID}",
            "//...",
        ),
        commit_sha=SOURCE,
    )


def failures(invocation: Invocation, *, run_id: str = RUN_ID) -> frozenset[str]:
    return recorded_test_failures(
        invocation,
        invocation_id_value=TEST_INVOCATION_ID,
        source_commit=SOURCE,
        current_commit=SOURCE,
        run_id=run_id,
        run_attempt=ATTEMPT,
        event_name="push",
        rbe_image=RBE_IMAGE,
    )


@pytest.mark.parametrize(
    ("status", "recorded"),
    [
        ("FAILED", True),
        ("TIMEOUT", True),
        ("FAILED_TO_BUILD", True),
        ("PASSED", False),
        ("REMOTE_FAILURE", False),
        ("INCOMPLETE", False),
    ],
)
def test_only_verdicts_a_rerun_would_repeat_are_recorded(status: str, recorded: bool) -> None:
    # The sweep as a whole failed, which is when these verdicts matter.
    sweep = push_sweep([summary("//runner:runner_test", status), summary("//other:test", "FAILED")])
    assert ("//runner:runner_test" in failures(sweep)) is recorded


def test_an_unfinished_sweep_records_nothing() -> None:
    assert failures(push_sweep([summary("//runner:runner_test", "FAILED")], finished=False)) == frozenset()


def test_another_runs_sweep_records_nothing() -> None:
    assert failures(push_sweep([summary("//runner:runner_test", "FAILED")]), run_id="1") == frozenset()


def test_a_selector_is_blocked_by_any_of_its_failed_labels() -> None:
    failed = frozenset({"//b:test"})
    assert selector_already_failed("//a:test //b:test", failed)[0]
    assert not selector_already_failed("//a:test", failed)[0]


if __name__ == "__main__":
    pytest_bazel.main()
