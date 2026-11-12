import dataclasses
import json
import os
import threading
from collections.abc import Callable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, TestSummary, read
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
        exit_code_name="SUCCESS" if all(s.status == "PASSED" for s in summaries) else "TESTS_FAILED",
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


def retry_run() -> dict[str, Invocation]:
    initial = dataclasses.replace(
        push_sweep([summary("//a:test", "FAILED")]),
        invocation_id=TEST_INVOCATION_ID,
        parent_invocation_id="runner",
        exit_code_name="REMOTE_CACHE_EVICTED",
    )
    retry = dataclasses.replace(
        push_sweep([summary("//a:test", "PASSED")]),
        invocation_id="retry",
        parent_invocation_id="runner",
        exit_code_name="SUCCESS",
    )
    return {
        TEST_INVOCATION_ID: initial,
        "retry": retry,
        "runner": Invocation(
            outputs=[],
            test_status={},
            invocation_id="runner",
            finished=True,
            child_invocation_ids=("query", TEST_INVOCATION_ID, "retry", "build"),
        ),
        "build": Invocation(outputs=[], test_status={}, command="build", invocation_id="build"),
    }


def load_retry_run(monkeypatch: pytest.MonkeyPatch, invocations: dict[str, Invocation]) -> TestEvidence:
    monkeypatch.setenv("GITHUB_SHA", SOURCE)
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", ATTEMPT)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")

    def read(identifier: str) -> Invocation:
        if identifier not in invocations:
            raise BuildBuddyError(f"unavailable: {identifier}")
        return invocations[identifier]

    monkeypatch.setattr(publication_evidence, "read", read)
    return publication_evidence.load(
        ["build"], test_invocation_id=TEST_INVOCATION_ID, source_commit=SOURCE, rbe_container_image=RBE_IMAGE
    ).tests


def test_cache_retry_reuses_final_pass_instead_of_suppressing_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = load_retry_run(monkeypatch, retry_run())
    assert evidence.is_covered("//a:test")[0]
    assert not evidence.already_failed("//a:test")[0]


def test_multiple_cache_retries_use_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = retry_run()
    invocations["cache-retry"] = dataclasses.replace(invocations[TEST_INVOCATION_ID], invocation_id="cache-retry")
    invocations["runner"] = dataclasses.replace(
        invocations["runner"], child_invocation_ids=(TEST_INVOCATION_ID, "cache-retry", "retry", "build")
    )
    assert load_retry_run(monkeypatch, invocations).is_covered("//a:test")[0]


def test_failed_final_retry_still_blocks_its_failed_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = retry_run()
    invocations["retry"] = dataclasses.replace(
        invocations["retry"],
        successful=False,
        test_summaries=[summary("//a:test", "FAILED")],
        exit_code_name="TESTS_FAILED",
    )
    evidence = load_retry_run(monkeypatch, invocations)
    assert not evidence.is_covered("//a:test")[0]
    assert evidence.already_failed("//a:test")[0]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda i: dataclasses.replace(i, parent_invocation_id="another-runner"), id="parent"),
        pytest.param(lambda i: dataclasses.replace(i, invocation_id="another-invocation"), id="identity"),
        pytest.param(lambda i: dataclasses.replace(i, commit_sha="another-source"), id="source"),
        pytest.param(lambda i: dataclasses.replace(i, requested_patterns=("//a:...",)), id="scope"),
        pytest.param(
            lambda i: dataclasses.replace(
                i, explicit_cmd_line=("test", f"--invocation_id={TEST_INVOCATION_ID}", "--config=local", "//...")
            ),
            id="options",
        ),
        pytest.param(lambda i: dataclasses.replace(i, finished=False), id="unfinished"),
        pytest.param(lambda i: dataclasses.replace(i, exit_code_name=None), id="missing-exit-code"),
    ],
)
def test_unproven_retry_keeps_the_test_gate_without_old_failure_suppression(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[Invocation], Invocation]
) -> None:
    invocations = retry_run()
    invocations["retry"] = change(invocations["retry"])
    evidence = load_retry_run(monkeypatch, invocations)
    assert not evidence.is_covered("//a:test")[0]
    assert not evidence.already_failed("//a:test")[0]


@pytest.mark.parametrize(
    "children",
    [
        ("retry", "build"),
        (TEST_INVOCATION_ID, TEST_INVOCATION_ID, "retry", "build"),
        (TEST_INVOCATION_ID, "unreadable", "retry", "build"),
        (TEST_INVOCATION_ID, "build"),
    ],
)
def test_incomplete_runner_lineage_does_not_reuse_a_neighboring_pass(
    monkeypatch: pytest.MonkeyPatch, children: tuple[str, ...]
) -> None:
    invocations = retry_run()
    invocations["runner"] = dataclasses.replace(invocations["runner"], child_invocation_ids=children)
    evidence = load_retry_run(monkeypatch, invocations)
    assert not evidence.is_covered("//a:test")[0]
    assert not evidence.already_failed("//a:test")[0]


def test_unfinished_parent_does_not_hide_a_later_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = retry_run()
    invocations["runner"] = dataclasses.replace(invocations["runner"], finished=False)
    assert not load_retry_run(monkeypatch, invocations).is_covered("//a:test")[0]


def test_older_export_without_exit_code_does_not_suppress_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = retry_run()
    invocations[TEST_INVOCATION_ID] = dataclasses.replace(invocations[TEST_INVOCATION_ID], exit_code_name=None)
    evidence = load_retry_run(monkeypatch, invocations)
    assert not evidence.is_covered("//a:test")[0]
    assert not evidence.already_failed("//a:test")[0]


def test_a_non_cache_result_cannot_be_followed_by_another_reusable_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = retry_run()
    invocations["later"] = dataclasses.replace(invocations["retry"], invocation_id="later")
    invocations["runner"] = dataclasses.replace(
        invocations["runner"], child_invocation_ids=(TEST_INVOCATION_ID, "retry", "later", "build")
    )
    assert not load_retry_run(monkeypatch, invocations).is_covered("//a:test")[0]


@pytest.mark.parametrize("retry_passed", [True, False])
def test_publication_reads_retry_evidence_through_the_real_go_exporter(
    monkeypatch: pytest.MonkeyPatch, retry_passed: bool
) -> None:
    invocations = retry_run()
    if not retry_passed:
        invocations["retry"] = dataclasses.replace(
            invocations["retry"],
            successful=False,
            test_summaries=[summary("//a:test", "FAILED")],
            exit_code_name="TESTS_FAILED",
        )
    streams: dict[str, bytes] = {}
    for identifier, invocation in invocations.items():
        events: list[dict[str, object]] = [{"started": {"uuid": identifier, "command": invocation.command}}]
        if invocation.parent_invocation_id:
            events.append({"buildMetadata": {"metadata": {"PARENT_INVOCATION_ID": invocation.parent_invocation_id}}})
        if invocation.explicit_cmd_line is not None:
            events.append({"optionsParsed": {"explicitCmdLine": list(invocation.explicit_cmd_line)}})
        if invocation.requested_patterns:
            events.append({"id": {"pattern": {"pattern": list(invocation.requested_patterns)}}})
        if invocation.commit_sha:
            events.append({"workspaceStatus": {"item": [{"key": "COMMIT_SHA", "value": invocation.commit_sha}]}})
        events.extend(
            {"childInvocationsConfigured": {"invocation": [{"invocationId": child}]}}
            for child in invocation.child_invocation_ids
        )
        events.extend(
            {
                "id": {"testSummary": {"label": result.label, "configuration": {"id": result.configuration_id}}},
                "testSummary": {"overallStatus": result.status, "attemptCount": result.attempt_count},
            }
            for result in invocation.test_summaries
        )
        if invocation.finished:
            events.append({"finished": {"exitCode": {"name": invocation.exit_code_name or "OK"}}})
        streams[identifier] = json.dumps(events).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlsplit(self.path).query)
            assert query["artifact"] == ["raw_json"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(streams[query["invocation_id"][0]])

        def log_message(self, format: str, *args: object) -> None:
            pass

    runfiles = Runfiles.Create()
    assert runfiles is not None
    binary = runfiles.Rlocation(os.environ["BBAPI_RLOCATION"])
    assert binary is not None
    monkeypatch.setenv("PATH", f"{Path(binary).parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "fixture-key")
    monkeypatch.setenv("GITHUB_SHA", SOURCE)
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", ATTEMPT)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            monkeypatch.setattr(
                publication_evidence, "read", partial(read, base_url=f"http://127.0.0.1:{server.server_port}")
            )
            evidence = publication_evidence.load(
                ["build"], test_invocation_id=TEST_INVOCATION_ID, source_commit=SOURCE, rbe_container_image=RBE_IMAGE
            ).tests
        finally:
            server.shutdown()
            thread.join()
    assert evidence.is_covered("//a:test")[0] is retry_passed
    assert evidence.already_failed("//a:test")[0] is not retry_passed


if __name__ == "__main__":
    pytest_bazel.main()
