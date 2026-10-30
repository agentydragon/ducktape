import dataclasses

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, TestSummary
from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.ci import publication_evidence
from devinfra.ci.publication_test_verdicts import TestEvidence, verify

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


def evidence_for(invocation: Invocation, *, run_id: str = RUN_ID) -> TestEvidence:
    return verify(
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
    assert ("//runner:runner_test" in evidence_for(sweep).failed_labels) is recorded


def test_an_unfinished_sweep_records_nothing() -> None:
    assert (
        evidence_for(push_sweep([summary("//runner:runner_test", "FAILED")], finished=False)).failed_labels
        == frozenset()
    )


def test_another_runs_sweep_records_nothing() -> None:
    assert (
        evidence_for(push_sweep([summary("//runner:runner_test", "FAILED")]), run_id="1").failed_labels == frozenset()
    )


def test_a_selector_is_blocked_by_any_of_its_failed_labels() -> None:
    evidence = TestEvidence(problem=None, failed_labels=frozenset({"//b:test"}))
    assert evidence.already_failed("//a:test //b:test")[0]
    assert not evidence.already_failed("//a:test")[0]


@pytest.mark.parametrize(
    "summaries", [[summary("//a:test", "PASSED")], [dataclasses.replace(summary("//a:test", "PASSED"), cached_count=1)]]
)
def test_a_verified_successful_sweep_can_supply_a_passing_verdict(summaries: list[TestSummary]) -> None:
    evidence = evidence_for(push_sweep(summaries))
    assert evidence.is_covered("//a:test")[0]
    assert not evidence.already_failed("//a:test")[0]


def test_passing_tests_from_a_failed_sweep_are_not_reused() -> None:
    evidence = evidence_for(push_sweep([summary("//a:test", "PASSED"), summary("//b:test", "FAILED")]))
    assert not evidence.is_covered("//a:test")[0]
    assert evidence.already_failed("//b:test")[0]


@pytest.mark.parametrize(
    "summaries",
    [
        [summary("//a:test", "PASSED"), summary("//a:test", "PASSED")],
        [dataclasses.replace(summary("//a:test", "PASSED"), attempt_count=2)],
        [dataclasses.replace(summary("//a:test", "PASSED"), configuration_id="")],
    ],
)
def test_ambiguous_or_retried_summaries_do_not_supply_a_passing_verdict(summaries: list[TestSummary]) -> None:
    assert not evidence_for(push_sweep(summaries)).is_covered("//a:test")[0]


@pytest.mark.parametrize("selector", ["//a:tests", "//a/...", "@external//a:test"])
def test_unproven_selectors_keep_their_test_gate(selector: str) -> None:
    assert not evidence_for(push_sweep([summary("//a:test", "PASSED")])).is_covered(selector)[0]


def test_loading_keeps_exact_test_evidence_separate_from_merged_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", SOURCE)
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", ATTEMPT)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    failed_test = push_sweep([summary("//a:test", "FAILED")])
    later_build = push_sweep([summary("//a:test", "PASSED")])
    calls = []

    def read(identifier: str) -> Invocation:
        calls.append(identifier)
        if identifier == "unreadable":
            raise BuildBuddyError("offline")
        return failed_test if identifier == TEST_INVOCATION_ID else later_build

    monkeypatch.setattr(publication_evidence, "read", read)
    evidence = publication_evidence.load(
        [TEST_INVOCATION_ID, "unreadable", "build", TEST_INVOCATION_ID],
        test_invocation_id=TEST_INVOCATION_ID,
        source_commit=SOURCE,
        rbe_container_image=RBE_IMAGE,
    )
    assert calls == [TEST_INVOCATION_ID, "unreadable", "build"]
    assert evidence.outputs is not None
    assert evidence.outputs.test_status["//a:test"] == "PASSED"
    assert evidence.tests.already_failed("//a:test")[0]
    assert not evidence.tests.is_covered("//a:test")[0]


def test_build_outputs_and_race_test_verdicts_are_loaded_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", SOURCE)
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", ATTEMPT)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    normal = Output(
        label="//app:binary", path="bin/app", uri="normal-uri", digest="normal", size=10, output_group="default"
    )
    instrumented = dataclasses.replace(normal, uri="race-uri", digest="race")
    build = Invocation(outputs=[normal], test_status={})
    tests = dataclasses.replace(push_sweep([summary("//a:test", "PASSED")]), outputs=[instrumented])
    calls = []

    def read(identifier: str) -> Invocation:
        calls.append(identifier)
        return tests if identifier == TEST_INVOCATION_ID else build

    monkeypatch.setattr(publication_evidence, "read", read)
    evidence = publication_evidence.load(
        ["build"], test_invocation_id=TEST_INVOCATION_ID, source_commit=SOURCE, rbe_container_image=RBE_IMAGE
    )
    assert calls == ["build", TEST_INVOCATION_ID]
    assert evidence.outputs is not None
    assert evidence.outputs.outputs == [normal]
    assert evidence.tests.is_covered("//a:test")[0]


def test_unreadable_test_invocation_keeps_test_gates_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def read(identifier: str) -> Invocation:
        if identifier == TEST_INVOCATION_ID:
            raise BuildBuddyError("offline")
        return push_sweep([summary("//a:test", "PASSED")])

    monkeypatch.setattr(publication_evidence, "read", read)
    evidence = publication_evidence.load(
        ["build"], test_invocation_id=TEST_INVOCATION_ID, source_commit=SOURCE, rbe_container_image=RBE_IMAGE
    )
    assert evidence.outputs is not None
    assert not evidence.tests.is_covered("//a:test")[0]
    assert not evidence.tests.already_failed("//a:test")[0]


if __name__ == "__main__":
    pytest_bazel.main()
