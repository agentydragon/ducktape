from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Literal, NoReturn

import pytest
import pytest_bazel
from botocore.exceptions import ClientError
from PIL import Image

from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.pr_visuals.artifacts import BuildBuddyArtifact, list_ci_artifacts
from devinfra.pr_visuals.collection import (
    CollectedOutputs,
    CollectionIssue,
    OutputKey,
    ResultCoverage,
    ResultKey,
    SourceSummary,
)
from devinfra.pr_visuals.comment_publishing import CommentUpdate, publish_pull_request_comment
from devinfra.pr_visuals.github_comment import (
    COMMENT_BUDGET,
    COMMENT_MARKER,
    error_comment_body,
    no_visual_comment_body,
    success_comment_body,
)
from devinfra.pr_visuals.metadata import (
    BaselineMetadata,
    BaselinePointer,
    ClassificationCounts,
    ReviewAsset,
    ReviewBundleMetadata,
    ReviewIssue,
    ReviewIssueSource,
    ReviewResult,
    ReviewTest,
)
from devinfra.pr_visuals.publisher import (
    S3_IMAGE_UPLOAD_WORKERS,
    DownloadedAsset,
    DownloadedVariant,
    DownloadedVisualTest,
    _invocations_for,
    build_bundle,
    diff_check,
    download_visual_tests,
    find_test_invocations,
    list_ci_failures,
    main,
    target_slug,
    upload_bundle,
    wait_for_test_invocations,
    write_baseline_pointers,
)
from devinfra.pr_visuals.pull_request import PullRequestRef
from devinfra.pr_visuals.testing.outputs import Blob, output_observation, result_artifact
from devinfra.testing.output_writer import BoundTestOutputs


def _png(path: Path, color: tuple[int, int, int, int] = (10, 20, 30, 255), size: tuple[int, int] = (8, 8)) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


class FakeBaselineSource:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str) -> bytes | None:
        return self.objects.get(key)


def test_the_full_sweep_wins_over_an_affected_set_run_at_the_same_commit() -> None:
    """The case that made #4927 insufficient: one commit, several CI runs. Only the
    `//...` sweep carries the visual manifests; an affected-set run at the same commit
    is complete and real and has none. Picking by commit must prefer the sweep."""
    command_seen: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        command_seen.append(words)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "invocation": [
                        {"invocationId": "affected-set", "pattern": ["//cluster/k8s:cluster_all_files"]},
                        {"invocationId": "full-sweep", "pattern": ["//..."]},
                    ]
                }
            ),
            "",
        )

    found = find_test_invocations(
        run_id="33066954750",
        run_attempt="1",
        commit_sha="5ee3b732cc68b03ef86a1b6e95598f54d796e8f2",
        repository="example/repo",
        run=fake_run,
    )
    assert found == ["full-sweep"]
    assert command_seen == [
        [
            "bbapi",
            "--timeout",
            "30s",
            "invocation",
            "list",
            "--repo",
            "https://github.com/example/repo",
            "--commit",
            "5ee3b732cc68b03ef86a1b6e95598f54d796e8f2",
            "--command",
            "test",
            "--role",
            "CI",
            "--count",
            "25",
            "--json",
        ]
    ]


def test_devel_uses_this_runs_ids_without_searching_other_runs() -> None:
    args = argparse.Namespace(ci_run_id="42975955001", ci_run_attempt="2", pull_request_head=None)

    def unexpected_search(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("devel must use its own full-sweep invocation")

    assert _invocations_for(args, run=unexpected_search) == [
        str(invocation_id(run_id="42975955001", attempt="2", role=role)) for role in ("test", "build")
    ]


def test_an_invocation_that_never_existed_is_not_a_failure() -> None:
    """A run cancelled before Bazel started names two invocations BuildBuddy has never
    seen, because the IDs are assigned up front. That is a normal empty result — raising
    would turn the quietest case into a red job."""
    calls = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="Error: HTTP 500: rpc error: code = NotFound desc = invocation not found",
    )
    assert list_ci_artifacts(["absent-a", "absent-b"], run=lambda *_a, **_k: calls) == []


def test_a_genuine_query_failure_still_raises() -> None:
    """A transport or auth failure must not read as 'this run had no visuals' — that
    would publish an empty bundle over a commit that really did render something."""
    broken = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Error: HTTP 503: upstream timeout")
    with pytest.raises(RuntimeError):
        list_ci_artifacts(["a", "b"], run=lambda *_a, **_k: broken)


def test_late_invocation_artifacts_are_read_only_after_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    candidate = _single_asset_test(tmp_path / "candidate", repository="agentydragon/ducktape")
    states = iter(["PARTIAL_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS"])
    complete = False
    listed_after_completion: list[bool] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "invocation" in words and words[words.index("invocation") + 1] == "list":
            return subprocess.CompletedProcess(
                command, 0, '{"invocation":[{"invocationId":"late-sweep","pattern":["//..."]}]}', ""
            )
        nonlocal complete
        state = next(states)
        complete = state == "COMPLETE_INVOCATION_STATUS"
        return subprocess.CompletedProcess(command, 0, json.dumps({"invocation": [{"invocationStatus": state}]}), "")

    def download(_invocations: list[str], _destination: Path, **_kwargs: object) -> list[DownloadedVisualTest]:
        listed_after_completion.append(complete)
        return [candidate] if complete else []

    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", fake_run)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", download)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.INVOCATION_POLL_SECONDS", 0)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.boto3.client", lambda *_a, **_k: EmptyBucket())
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upload_bundle", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.find_reviewable_pull_request", lambda **_k: PullRequestRef(8733, "c" * 40)
    )

    main()

    assert listed_after_completion == [True]
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "success"]


def test_incomplete_invocation_cannot_advance_a_devel_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr("sys.argv", _publisher_argv(tmp_path / "work"))
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, '{"invocation":[]}', ""),
    )
    monkeypatch.setattr("devinfra.pr_visuals.publisher.wait_for_test_invocations", lambda *_a, **_k: "incomplete")
    _forbid(monkeypatch, "download_visual_tests", "upload_bundle", "write_baseline_pointers")

    with pytest.raises(RuntimeError, match="did not complete"):
        main()

    assert [check["conclusion"] for check in publisher_run.checks] == ["failure"]


@pytest.mark.parametrize("first", [None, "PARTIAL_INVOCATION_STATUS", "DISCONNECTED_INVOCATION_STATUS"])
def test_wait_for_invocations_uses_terminal_state(first: str | None) -> None:
    statuses = iter([first, "COMPLETE_INVOCATION_STATUS"])
    waits: list[float] = []
    assert (
        wait_for_test_invocations(
            ["known"],
            run=lambda command, **_kwargs: subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"invocation": [{"invocationStatus": state}] if (state := next(statuses)) else []}),
                "",
            ),
            monotonic=lambda: 0,
            wait=waits.append,
        )
        == "complete"
    )
    assert waits == [2]


def test_wait_for_invocations_has_a_bounded_deadline() -> None:
    clock = iter([0, 300])
    assert (
        wait_for_test_invocations(
            ["still-running"],
            run=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, '{"invocation":[{"invocationStatus":"PARTIAL_INVOCATION_STATUS"}]}', ""
            ),
            monotonic=lambda: next(clock),
            wait=lambda _seconds: pytest.fail("the deadline elapsed"),
        )
        == "incomplete"
    )


def test_failed_workflow_does_not_treat_missing_invocations_as_no_tests() -> None:
    states = iter([None, "PARTIAL_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS"])
    waits: list[float] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        selected = words[words.index("status") + 1]
        if selected == "build":
            return subprocess.CompletedProcess(command, 0, "{}", "")
        state = next(states)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"invocation": [{"invocationStatus": state}] if state else []}), ""
        )

    assert (
        wait_for_test_invocations(
            ["test"],
            allow_build_only=True,
            build_invocation_id="build",
            run=fake_run,
            monotonic=lambda: 0,
            wait=waits.append,
        )
        == "complete"
    )
    assert waits == [2, 2]


def test_successful_build_only_run_has_no_visuals_without_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr("sys.argv", _publisher_argv(tmp_path / "work"))

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "invocation" in words and words[words.index("invocation") + 1] == "list":
            return subprocess.CompletedProcess(command, 0, '{"invocation":[]}', "")
        selector = words[words.index("status") + 1]
        build_id = str(invocation_id(run_id="33060467222", attempt="1", role="build"))
        return subprocess.CompletedProcess(
            command,
            0,
            '{"invocation":[{"invocationStatus":"COMPLETE_INVOCATION_STATUS"}]}'
            if selector == build_id
            else '{"invocation":[]}',
            "",
        )

    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", fake_run)
    _forbid(monkeypatch, "download_visual_tests", "upload_bundle", "write_baseline_pointers")

    main()

    assert [check["conclusion"] for check in publisher_run.checks] == ["neutral"]


def _download_runner(blobs: dict[str, bytes]) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
        for item in manifest:
            Path(item["destination"]).write_bytes(blobs[item["uri"]])
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def test_download_preserves_case_and_checkpoint_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = b"png"
    observations = tuple(
        output_observation(
            body,
            key=OutputKey("owner/repo", "//ui:visual", case_id, None, "screenshot"),
            result=ResultKey("invocation", "//ui:visual", "cfg", 1, 1, 1),
        )
        for case_id in ("a/test_visual.py::test_same", "b/test_visual.py::test_same")
    )
    coverage = ResultCoverage(observations[0].result, "PASSED", "PASSED", 2, True, ())
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs(observations, observations, (coverage,), (), True),
    )
    tests = download_visual_tests(
        ["invocation"], tmp_path / "tests", repository="owner/repo", run=_download_runner({Blob(body).uri: body})
    )
    assert len(tests) == 1
    assert {asset.case_id for asset in tests[0].assets} == {
        "a/test_visual.py::test_same",
        "b/test_visual.py::test_same",
    }
    assert {asset.checkpoint for asset in tests[0].assets} == {None}
    assert len({asset.path for asset in tests[0].assets}) == 2
    assert all(asset.variants[0].path.read_bytes() == body for asset in tests[0].assets)


def test_download_marks_variant_and_target_status_issues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first, second = b"one", b"two"
    observations = tuple(
        output_observation(
            body,
            key=OutputKey("owner/repo", "//ui:visual", "a/test_visual.py::test_same", "expanded", "screenshot"),
            result=ResultKey("invocation", "//ui:visual", "cfg", run_number, 1, 1),
        )
        for run_number, body in enumerate((first, second), start=1)
    )
    coverage = (
        ResultCoverage(
            observations[0].result,
            "PASSED",
            "FAILED",
            1,
            True,
            (CollectionIssue("target overall status FAILED", result=observations[0].result),),
        ),
        ResultCoverage(
            observations[1].result,
            "PASSED",
            "FAILED",
            1,
            True,
            (CollectionIssue("target overall status FAILED", result=observations[1].result),),
        ),
    )
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs(
            observations,
            observations,
            coverage,
            (CollectionIssue("target overall status FAILED", target="//ui:visual"),),
            False,
        ),
    )
    tests = download_visual_tests(
        ["invocation"],
        tmp_path / "tests",
        repository="owner/repo",
        run=_download_runner({Blob(body).uri: body for body in (first, second)}),
    )
    assert len(tests[0].assets) == 1
    assert len(tests[0].assets[0].variants) == 2
    assert tests[0].assets[0].checkpoint == "expanded"
    assert any("successfully" in issue.message for issue in tests[0].issues)


def test_failed_shards_are_grouped_and_linked_in_comment_and_gallery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invocation = "d1fafe09-e2cf-5409-8fbc-7ab0308820c4"
    configuration_one = "abcdef01" + "1" * 56
    configuration_two = "abcdef01" + "2" * 56
    message = "terminal attempt has no confirmed successful result (status=FAILED)"
    results = []
    for shard in (1, 2, 3, 5, 10, 11):
        result = ResultKey(invocation, "//ui:visual", configuration_one, 1, shard, 1)
        results.append(
            ResultCoverage(
                result,
                "FAILED",
                "FAILED",
                1,
                False,
                (CollectionIssue(message, result=result), CollectionIssue("bad <script>", result=result)),
            )
        )
    second_invocation = "d1fafe09-extra"
    second_result = ResultKey(second_invocation, "//ui:visual", configuration_two, 1, 1, 1)
    results.append(
        ResultCoverage(
            second_result, "FAILED", "FAILED", 1, False, (CollectionIssue("another failure", result=second_result),)
        )
    )
    results_tuple = tuple(results)
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs((), (), results_tuple, (), False),
    )
    downloaded = download_visual_tests([invocation], tmp_path / "downloads", repository="owner/repo")
    bundle = build_bundle(
        downloaded, tmp_path / "site", commit_sha="b" * 40, repository="owner/repo", base_sha="a" * 40
    )
    test = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests[0]
    comment = success_comment_body(
        repository="owner/repo", commit_sha="b" * 40, url="https://visuals/", review_tests=[test], base_sha="a" * 40
    )
    page = (bundle / "tests" / test.slug / "index.html").read_text()
    for rendered in (comment, page):
        assert rendered.count(message) == 1
        assert "shards 1-3, 5, 10-11" in rendered
        assert f'href="https://app.buildbuddy.io/invocation/{invocation}"' in rendered
        assert f">BuildBuddy {invocation[:11]}</a>" in rendered
        assert (
            f'href="https://app.buildbuddy.io/invocation/{second_invocation}">BuildBuddy {second_invocation[:11]}</a>'
            in rendered
        )
        assert "config abcdef011" in rendered
        assert "config abcdef012" in rendered
        assert "another failure" in rendered
        assert "bad &lt;script&gt;" in rendered
        assert "bad <script>" not in rendered
        assert "ResultKey(" not in rendered
        assert configuration_one not in rendered
        assert configuration_two not in rendered
    assert not test.baseline_eligible


def test_cross_shard_issue_keeps_output_identity_and_source_shards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invocation = "d1fafe09-e2cf-5409-8fbc-7ab0308820c4"
    first = ResultKey(invocation, "//ui:visual", "config-a", 1, 1, 1)
    second = ResultKey(invocation, "//ui:visual", "config-a", 1, 2, 1)
    output = OutputKey("owner/repo", "//ui:visual", "dialog/mobile", "expanded", "screenshot")
    issue = CollectionIssue(
        "output identity also claimed across shards", result=second, related_results=(first,), output=output
    )
    coverage = ResultCoverage(second, "PASSED", "PASSED", 1, False, (issue,))
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs((), (), (coverage,), (), False),
    )

    test = download_visual_tests([invocation], tmp_path / "downloads", repository="owner/repo")[0]

    assert len(test.issues) == 1
    assert test.issues[0].message == (
        "output identity also claimed across shards: case 'dialog/mobile', checkpoint 'expanded', role 'screenshot'"
    )
    assert test.issues[0].source is not None
    assert test.issues[0].source.shards == [1, 2]
    assert "OutputKey(" not in test.issues[0].message


def test_duplicate_output_issue_keeps_record_filename_in_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = ResultKey("invocation", "//ui:visual", "config-a", 1, 1, 1)
    output = OutputKey("owner/repo", "//ui:visual", "dialog/mobile", "expanded", "screenshot")
    issue = CollectionIssue(
        "duplicate output identity",
        result=result,
        output=output,
        record="test.outputs/test-outputs/records/duplicate.json",
    )
    coverage = ResultCoverage(result, "PASSED", "PASSED", 2, False, (issue,))
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs((), (), (coverage,), (), False),
    )

    test = download_visual_tests(["invocation"], tmp_path / "downloads", repository="owner/repo")[0]

    assert [entry.message for entry in test.issues] == [
        "duplicate output identity: case 'dialog/mobile', checkpoint 'expanded', role 'screenshot', "
        "record 'test.outputs/test-outputs/records/duplicate.json'"
    ]


def test_unstable_variant_provenance_groups_shards_and_uses_distinguishable_links(tmp_path: Path) -> None:
    image_one = _png(tmp_path / "first.png", (10, 20, 30, 255))
    image_two = _png(tmp_path / "second.png", (40, 50, 60, 255))
    image_three = _png(tmp_path / "third.png", (70, 80, 90, 255))
    image_four = _png(tmp_path / "fourth.png", (100, 110, 120, 255))
    invocation_one = "d1fafe09"
    invocation_two = "d1fafe09-2222-2222-2222-222222222222"
    invocation_three = "d1fafe09-extra-long"
    invocation_four = "d1fafe09-other-long"
    invocation_five = "d1fafe09-issue-long"
    configuration_one = "abcdef01" + "1" * 56
    configuration_two = "abcdef01" + "2" * 56
    configuration_three = "abcdef01" + "3" * 56
    configuration_four = "abcdef01" + "4" * 56
    configuration_five = "abcdef01" + "5" * 56

    def result(invocation: str, configuration: str, run: int, shard: int, attempt: int) -> ReviewResult:
        return ReviewResult(
            invocation=invocation, configuration=configuration, run=run, shard=shard, attempt=attempt, status="PASSED"
        )

    label = "//example:visuals"
    test = DownloadedVisualTest(
        label,
        target_slug(label, repository="owner/repo"),
        "Visual states",
        [
            DownloadedAsset(
                "ui/test_visual.py::test_dialog",
                None,
                "screenshot",
                "Dialog",
                [
                    DownloadedVariant(
                        image_one,
                        hashlib.sha256(image_one.read_bytes()).hexdigest(),
                        [result(invocation_one, configuration_one, 2, shard, 1) for shard in (1, 2, 3)],
                    ),
                    DownloadedVariant(
                        image_two,
                        hashlib.sha256(image_two.read_bytes()).hexdigest(),
                        [result(invocation_two, configuration_two, 3, 1, 2)],
                    ),
                ],
            ),
            DownloadedAsset(
                "ui/test_visual.py::test_menu",
                None,
                "screenshot",
                "Menu",
                [
                    DownloadedVariant(
                        image_three,
                        hashlib.sha256(image_three.read_bytes()).hexdigest(),
                        [result(invocation_three, configuration_three, 4, 2, 1)],
                    ),
                    DownloadedVariant(
                        image_four,
                        hashlib.sha256(image_four.read_bytes()).hexdigest(),
                        [result(invocation_four, configuration_four, 4, 3, 1)],
                    ),
                ],
            ),
        ],
        configuration_one,
        [
            ReviewIssue(
                message="related <invocation>",
                source=ReviewIssueSource(
                    invocation=invocation_five, configuration=configuration_five, run=5, shards=[2], attempt=1
                ),
            )
        ],
    )
    base_sha = "a" * 40
    bundle = build_bundle([test], tmp_path / "site", commit_sha="b" * 40, repository="owner/repo", base_sha=base_sha)
    metadata = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text())
    page = (bundle / "tests" / test.slug / "index.html").read_text()
    comment = success_comment_body(
        repository="owner/repo",
        commit_sha="b" * 40,
        url="https://visuals/commits/head/",
        review_tests=metadata.tests,
        base_sha=base_sha,
    )

    for rendered in (page, comment):
        assert "related &lt;invocation&gt;" in rendered
        assert "related <invocation>" not in rendered
        assert "run 2 · shards 1-3 · attempt 1 · config abcdef011" in rendered
        assert "run 3 · shard 1 · attempt 2 · config abcdef012" in rendered
        assert "run 4 · shard 2 · attempt 1 · config abcdef013" in rendered
        assert "run 4 · shard 3 · attempt 1 · config abcdef014" in rendered
        assert "shard 2 · run 5 · attempt 1 · config abcdef015" in rendered
        assert (
            f'href="https://app.buildbuddy.io/invocation/{invocation_one}">BuildBuddy {invocation_one}</a>' in rendered
        )
        assert (
            f'href="https://app.buildbuddy.io/invocation/{invocation_two}">BuildBuddy {invocation_two[:10]}</a>'
            in rendered
        )
        assert (
            f'href="https://app.buildbuddy.io/invocation/{invocation_three}">BuildBuddy {invocation_three[:10]}</a>'
            in rendered
        )
        assert (
            f'href="https://app.buildbuddy.io/invocation/{invocation_four}">BuildBuddy {invocation_four[:10]}</a>'
            in rendered
        )
        assert (
            f'href="https://app.buildbuddy.io/invocation/{invocation_five}">BuildBuddy {invocation_five[:10]}</a>'
            in rendered
        )
        assert configuration_one not in rendered
        assert configuration_two not in rendered
        assert configuration_three not in rendered
        assert configuration_four not in rendered
        assert configuration_five not in rendered
        assert rendered.count("shards 1-3") == 1


def test_fallback_target_title_is_not_repeated_in_gallery_or_comment(tmp_path: Path) -> None:
    label = "//example:visuals"
    source = _single_asset_test(tmp_path / "source", label=label, repository="owner/repo")
    source = DownloadedVisualTest(source.target_label, source.slug, label, source.assets, source.configuration, [])
    base_sha = "a" * 40
    bundle = build_bundle([source], tmp_path / "site", commit_sha="b" * 40, repository="owner/repo", base_sha=base_sha)
    metadata = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text())
    index = (bundle / "index.html").read_text()
    page = (bundle / "tests" / source.slug / "index.html").read_text()
    asset = metadata.tests[0].assets[0].model_copy(update={"classification": "new"})
    comment_test = metadata.tests[0].model_copy(update={"assets": [asset], "summary": ClassificationCounts(new=1)})
    comment = success_comment_body(
        repository="owner/repo",
        commit_sha="b" * 40,
        url="https://visuals/commits/head/",
        review_tests=[comment_test],
        base_sha=base_sha,
        ci_details_url="https://github.test/runs/123",
    )
    target_summary = comment.split("<summary>", 1)[1].split("</summary>", 1)[0]
    detail_header = page.split("<header>", 1)[1].split("</header>", 1)[0]
    index_summary = index.split("<p>", 1)[1].split("</p>", 1)[0]

    assert f'<h2><a href="tests/{source.slug}/index.html"><code>{label}</code></a></h2>' in index
    assert index_summary.lstrip().startswith("0 modified")
    assert detail_header.count(label) == 1
    assert target_summary.count(label) == 1
    assert target_summary.count("<a ") == 2
    assert ">link</a>" in target_summary
    assert ">request rerun</a>" in target_summary


def test_retry_notice_does_not_reclassify_an_issue_by_its_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result_key = ResultKey("invocation", "//ui:visual", "cfg", 1, 1, 1)
    result = ResultCoverage(
        result_key,
        "FAILED",
        "FLAKY",
        1,
        False,
        (CollectionIssue("superseded by retry", result=result_key),),
        retried_by=2,
    )
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs((), (), (result,), (), False),
    )
    test = download_visual_tests(["invocation"], tmp_path / "downloads", repository="owner/repo")[0]
    assert [issue.message for issue in test.issues] == ["superseded by retry"]
    assert [notice.message for notice in test.notices] == ["superseded by attempt 2"]


def test_download_rejects_ambiguous_configurations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observations = tuple(
        output_observation(
            body,
            key=OutputKey("owner/repo", "//ui:visual", "a/test_visual.py::test_same", None, "screenshot"),
            result=ResultKey("invocation", "//ui:visual", configuration, 1, 1, 1),
        )
        for configuration, body in (("cfg", b"one"), ("other", b"two"))
    )
    coverage = tuple(ResultCoverage(item.result, "PASSED", "PASSED", 1, True, ()) for item in observations)
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.collect_outputs",
        lambda *_a, **_k: CollectedOutputs(observations, observations, coverage, (), True),
    )
    tests = download_visual_tests(
        ["invocation"],
        tmp_path / "tests",
        repository="owner/repo",
        run=_download_runner({Blob(body).uri: body for body in (b"one", b"two")}),
    )
    assert tests[0].configuration is None
    assert any("configurations" in issue.message for issue in tests[0].issues)


def test_produced_outputs_survive_collection_and_content_addressed_publication(tmp_path: Path) -> None:
    image = _png(tmp_path / "source.png").read_bytes()
    failed_image = _png(tmp_path / "failed.png", color=(255, 0, 0, 255)).read_bytes()
    artifacts: dict[str, list[BuildBuddyArtifact]] = {}
    blobs: dict[str, bytes] = {}
    downloaded: set[str] = set()
    identities = {("dialog/mobile", None), ("dialog/mobile", "expanded"), ("dialog/desktop", None)}
    specs = [
        ("first", 1, 1, "FAILED"),
        ("first", 1, 2, "PASSED"),
        ("first", 2, 1, "PASSED"),
        ("second", 1, 1, "PASSED"),
        ("second", 2, 1, "PASSED"),
    ]
    for invocation, shard, attempt, status in specs:
        root = tmp_path / "produced" / invocation / str(shard) / str(attempt)
        case_id = "dialog/mobile" if shard == 1 else "dialog/desktop"
        outputs = BoundTestOutputs(output_dir=root, case_id=case_id)
        for checkpoint in [None, "expanded"] if shard == 1 else [None]:
            outputs.publish(
                checkpoint=checkpoint,
                role="screenshot",
                data=failed_image if status == "FAILED" else image,
                suffix="png",
                title="Dialog states",
            )
        outputs.publish(role="trace", data=b"diagnostics", suffix="zip")
        result = ResultKey(invocation, "//ui:visual", "config-a", 1, shard, attempt)
        summary = SourceSummary("FLAKY", 1, 2, 2, 3) if invocation == "first" else SourceSummary("PASSED", 1, 2, 1, 2)
        for path in sorted((root / "test-outputs").rglob("*")):
            if not path.is_file() or ".locks" in path.parts:
                continue
            blob = Blob(path.read_bytes())
            blobs[blob.uri] = blob.contents
            artifacts.setdefault(invocation, []).append(
                result_artifact(
                    "test.outputs/" + path.relative_to(root).as_posix(),
                    blob.uri,
                    result=result,
                    summary=summary,
                    status=status,
                )
            )

    def run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "artifact" in words:
            invocation = words[words.index("list") + 1]
            return subprocess.CompletedProcess(
                command, 0, json.dumps([item.model_dump(by_alias=True) for item in artifacts[invocation]]), ""
            )
        manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
        for item in manifest:
            downloaded.add(item["uri"])
            Path(item["destination"]).write_bytes(blobs[item["uri"]])
        return subprocess.CompletedProcess(command, 0, "", "")

    tests = download_visual_tests(["first", "second"], tmp_path / "downloaded", repository="owner/repo", run=run)
    bundle = build_bundle(
        tests,
        tmp_path / "site",
        commit_sha="a" * 40,
        repository="owner/repo",
        base_sha="b" * 40,
        baseline_source=FakeBaselineSource({}),
    )
    review = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_bytes()).tests[0]
    assert review.baseline_eligible
    assert not review.issues
    assert review.notices
    assert {(asset.case_id, asset.checkpoint) for asset in review.assets} == identities
    assert len({asset.path for asset in review.assets}) == len(identities)
    assert all(asset.classification == "new" for asset in review.assets)
    for asset in review.assets:
        assert len(asset.variants) == 1
        variant = asset.variants[0]
        assert {(result.invocation, result.shard, result.attempt) for result in variant.results} == {
            ("first", 1 if asset.case_id == "dialog/mobile" else 2, 2 if asset.case_id == "dialog/mobile" else 1),
            ("second", 1 if asset.case_id == "dialog/mobile" else 2, 1),
        }
        assert (tmp_path / "site" / "assets" / f"{variant.sha256}.png").read_bytes() == image
    assert Blob(image).uri in downloaded
    assert Blob(failed_image).uri not in downloaded
    assert Blob(b"diagnostics").uri not in downloaded
    assert len(list((tmp_path / "site" / "assets").iterdir())) == 1


def test_list_ci_failures_is_best_effort_and_deduplicates_labels() -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[2] == "missing":
            return subprocess.CompletedProcess(command, 1, "", "not found")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "targetGroups": [
                        {
                            "targets": [
                                {"metadata": {"label": "//z:test"}, "status": "FAILED"},
                                {"metadata": {"label": "//a:test"}, "status": "FAILED"},
                                {"metadata": {"label": "//ok:test"}, "status": "PASSED"},
                            ]
                        }
                    ]
                }
            ),
            "",
        )

    assert list_ci_failures(["missing", "invocation", "invocation"], run=fake_run) == ["//a:test", "//z:test"]


def test_build_bundle_groups_tests_and_writes_target_pages(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image = _png(source / "screen.png")
    test = _test_from_files("//example:visuals", "Example UI", [image], repository="agentydragon/ducktape")
    sha = "0123456789abcdef0123456789abcdef01234567"

    bundle = build_bundle([test], tmp_path / "site", commit_sha=sha, repository="agentydragon/ducktape")

    assert bundle == tmp_path / "site" / "reviews-test-outputs" / sha
    assert "//example:visuals" in (bundle / "index.html").read_text()
    target_page = bundle / "tests" / test.slug / "index.html"
    assert f'id="{test.assets[0].path}"' in target_page.read_text()
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    assert (tmp_path / "site" / "assets" / f"{digest}.png").read_bytes() == image.read_bytes()
    assert f"../../../../assets/{digest}.png" in target_page.read_text()
    assert not list(bundle.rglob("*.png"))


def test_build_bundle_rejects_abbreviated_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        build_bundle([], tmp_path, commit_sha="0123456", repository="repo")


def test_target_slug_is_isolated_by_repository() -> None:
    label = "//example:visuals"
    first = target_slug(label, repository="owner/one")
    second = target_slug(label, repository="owner/two")
    assert first != second
    assert first.startswith("example-visuals-")


def test_comment_bodies_link_commit_targets_and_report_errors() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(
            target_label="//example:visuals",
            slug=target_slug("//example:visuals", repository="agentydragon/ducktape"),
            title="Example UI",
            assets=[ReviewAsset(path="screen.png", label="Screen", case_id="ui/test_visual.py::test_screen")],
        )
    ]

    success = success_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        url="https://visuals/commits/sha/",
        review_tests=review_tests,
    )
    failure = error_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        error=ValueError("//example:visuals references missing artifact screen.png"),
    )

    assert "commit/0123456789abcdef0123456789abcdef01234567" in success
    assert "https://visuals/commits/sha/index.html" in success
    assert "tests/example-visuals-" in success
    assert "missing artifact screen.png" in failure

    warning = success_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        url="https://visuals/commits/sha/",
        review_tests=review_tests,
        ci_conclusion="failure",
        ci_failures=["//haku/console:x_test"],
    )
    assert "//haku/console:x_test" in warning

    no_visuals = no_visual_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        ci_conclusion="failure",
        ci_failures=["//haku/console:x_test"],
        details_url="https://github/actions/runs/1",
    )
    assert "https://github/actions/runs/1" in no_visuals


HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPOSITORY = "agentydragon/ducktape"


def _success_comment(commit_sha: str) -> str:
    return success_comment_body(
        repository=REPOSITORY, commit_sha=commit_sha, url="https://visuals/commits/sha/", review_tests=[]
    )


# Each existing body comes from the function that writes it, so this pins that the refresh
# recognizes what the publisher actually posts, whatever its wording.
@pytest.mark.parametrize(
    ("existing_body", "mode", "expected_edit", "expected_create"),
    [
        pytest.param(
            error_comment_body(repository=REPOSITORY, commit_sha=HEAD_SHA, error=ValueError("boom")),
            "refresh_stale",
            True,
            False,
            id="failure-of-this-commit",
        ),
        pytest.param(_success_comment(OTHER_SHA), "refresh_stale", True, False, id="success-of-another-commit"),
        pytest.param(_success_comment(HEAD_SHA), "refresh_stale", False, False, id="success-of-this-commit"),
        pytest.param(
            f"<!-- pr-visuals -->\n## Visual review for [`{HEAD_SHA[:8]}`]"
            f"(https://github.com/{REPOSITORY}/commit/{HEAD_SHA})\n"
            "[Open visual review](https://visuals/commits/sha/index.html)",
            "refresh_stale",
            False,
            False,
            id="previous-markdown-success-of-this-commit",
        ),
        pytest.param(
            no_visual_comment_body(
                repository=REPOSITORY, commit_sha=HEAD_SHA, ci_conclusion="failure", ci_failures=[], details_url=None
            ),
            "refresh_stale",
            True,
            False,
            id="no-artifacts-for-this-commit",
        ),
        pytest.param(None, "refresh_stale", False, False, id="no-comment"),
        pytest.param(_success_comment(HEAD_SHA), "replace", True, False, id="replace-existing"),
        pytest.param(None, "replace", False, True, id="create-missing"),
    ],
)
def test_publish_pull_request_comment(
    monkeypatch: pytest.MonkeyPatch,
    existing_body: str | None,
    mode: Literal["replace", "refresh_stale"],
    expected_edit: bool,
    expected_create: bool,
) -> None:
    edits: list[str] = []
    created: list[str] = []

    class FakeComment:
        body = existing_body
        user = type("User", (), {"type": "Bot"})()

        def edit(self, body: str) -> None:
            edits.append(body)

    class FakeIssue:
        def get_comments(self) -> list[FakeComment]:
            return [] if existing_body is None else [FakeComment()]

        def create_comment(self, body: str) -> None:
            created.append(body)

    class FakeGithub:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeGithub:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get_repo(self, _repository: str) -> FakeGithub:
            return self

        def get_issue(self, _pull_request: int) -> FakeIssue:
            return FakeIssue()

    monkeypatch.setattr("devinfra.pr_visuals.comment_publishing.Github", FakeGithub)
    publish_pull_request_comment(
        repository=REPOSITORY,
        pull_request=4594,
        commit_sha=HEAD_SHA,
        update=CommentUpdate(body="replacement", mode=mode),
        token="token",
    )

    assert edits == (["replacement"] if expected_edit else [])
    assert created == (["replacement"] if expected_create else [])


PR_HEAD_REF = "pr-owner:topic"


def _publisher_argv(work_dir: Path, *options: str) -> list[str]:
    return [
        "publisher",
        "--ci-run-id",
        "33060467222",
        "--ci-run-attempt",
        "1",
        "--work-dir",
        str(work_dir),
        "--sha",
        HEAD_SHA,
        "--repository",
        "agentydragon/ducktape",
        "--endpoint",
        "https://s3.example",
        "--bucket",
        "pr-visuals",
        "--public-base-url",
        "https://s3.example/pr-visuals",
        *options,
    ]


def _forbid(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Fail the test if `publisher.main` reaches any of these."""

    def forbidden(name: str) -> Callable[..., NoReturn]:
        def fail(*_args: object, **_kwargs: object) -> NoReturn:
            pytest.fail(f"the publisher must not reach {name}")

        return fail

    for name in names:
        monkeypatch.setattr(f"devinfra.pr_visuals.publisher.{name}", forbidden(name))


@pytest.mark.parametrize("pull_request_head", [None, PR_HEAD_REF])
def test_cancelled_run_closes_its_check_without_reading_or_publishing_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pull_request_head: str | None
) -> None:
    checks: list[dict[str, object]] = []
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upsert_check_run", lambda **kwargs: checks.append(kwargs))
    _forbid(
        monkeypatch,
        "find_reviewable_pull_request",
        "publish_pull_request_comment",
        "wait_for_test_invocations",
        "download_visual_tests",
        "build_bundle",
        "upload_bundle",
        "write_baseline_pointers",
        "_invocations_for",
    )
    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", lambda *_a, **_k: pytest.fail("Bazel read"))
    monkeypatch.setattr("devinfra.pr_visuals.publisher.boto3.client", lambda *_a, **_k: pytest.fail("S3 client"))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        "sys.argv",
        _publisher_argv(
            tmp_path / "work",
            "--ci-conclusion",
            "cancelled",
            *(["--pull-request-head", pull_request_head] if pull_request_head else []),
        ),
    )

    main()

    assert len(checks) == 1
    assert checks[0]["conclusion"] == "cancelled"
    assert checks[0]["commit_sha"] == HEAD_SHA


PR_CHECK_ID = "pr-visual-review:33060467222:1"


@dataclass
class PublisherRun:
    checks: list[dict[str, object]] = field(default_factory=list)
    comments: list[dict[str, object]] = field(default_factory=list)


@pytest.fixture
def publisher_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PublisherRun:
    """`main()` as the publish workflow starts it for a PR's CI run, recording its GitHub writes.

    The run's event is not trusted to name its pull request, so the publisher is handed the run's head ref
    instead. A test of another kind of run replaces `sys.argv`.
    """
    run = PublisherRun()
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upsert_check_run", lambda **kwargs: run.checks.append(kwargs))
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.publish_pull_request_comment", lambda **kwargs: run.comments.append(kwargs)
    )

    # The PR head is absent from invocation search (CI records the merge SHA), but
    # its derived test invocation is complete when publication begins.
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "invocation" in words and words[words.index("invocation") + 1] == "list":
            output = '{"invocation": []}'
        elif "status" in words:
            output = '{"invocation":[{"invocationStatus":"COMPLETE_INVOCATION_STATUS"}]}'
        elif "artifact" in words:
            output = "[]"
        else:
            raise AssertionError(f"unexpected command {words}")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        _publisher_argv(tmp_path / "work", "--pull-request-head", PR_HEAD_REF, "--check-external-id", PR_CHECK_ID),
    )
    return run


class EmptyBucket:
    """An S3 bucket nothing was ever published to, remembering which keys it was asked for."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def get_object(self, **kwargs: str) -> NoReturn:
        self.requested.append(kwargs["Key"])
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


def _publish_one_visual_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[DownloadedVisualTest, EmptyBucket]:
    """The run's Bazel invocation holds one visual target, and the bucket holds nothing yet."""
    candidate = _single_asset_test(tmp_path / "candidate", repository="agentydragon/ducktape")
    bucket = EmptyBucket()
    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr("devinfra.pr_visuals.publisher.boto3.client", lambda *_args, **_kwargs: bucket)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upload_bundle", lambda *_args, **_kwargs: None)
    return candidate, bucket


def test_a_pr_run_publishes_to_the_pr_its_head_ref_finds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    """The PR and its base commit come from the lookup, never from the run's event. The run then comments
    on that PR, compares against that base and — being a PR run — leaves the devel baseline pointers alone."""
    base_sha = "c" * 40
    lookups: list[dict[str, str]] = []

    def find(**kwargs: str) -> PullRequestRef:
        lookups.append(kwargs)
        return PullRequestRef(number=8733, base_sha=base_sha)

    candidate, bucket = _publish_one_visual_target(monkeypatch, tmp_path)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", find)
    _forbid(monkeypatch, "write_baseline_pointers")

    main()

    assert [(lookup["head"], lookup["head_sha"]) for lookup in lookups] == [(PR_HEAD_REF, HEAD_SHA)] * 2
    assert [comment["pull_request"] for comment in publisher_run.comments] == [8733]
    assert bucket.requested[:2] == [
        f"reviews-test-outputs/{base_sha}/tests/{candidate.slug}/metadata.json",
        f"baselines/reviews-test-outputs/{candidate.slug}.json",
    ]
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "success"]


@pytest.mark.parametrize("current", [None, PullRequestRef(number=9999, base_sha="c" * 40)])
def test_a_pr_that_changes_during_publication_does_not_get_a_stale_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun, current: PullRequestRef | None
) -> None:
    _publish_one_visual_target(monkeypatch, tmp_path)
    lookups = iter([PullRequestRef(number=8733, base_sha="c" * 40), current])
    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", lambda **_kwargs: next(lookups))
    _forbid(monkeypatch, "write_baseline_pointers")

    main()

    assert publisher_run.comments == []
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "success"]


def test_successful_pr_run_without_visuals_only_refreshes_a_stale_comment(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.find_reviewable_pull_request",
        lambda **_kwargs: PullRequestRef(number=8733, base_sha="c" * 40),
    )

    main()

    assert len(publisher_run.comments) == 1
    assert publisher_run.comments[0]["pull_request"] == 8733
    update = publisher_run.comments[0]["update"]
    assert isinstance(update, CommentUpdate)
    assert update.mode == "refresh_stale"
    assert [check["conclusion"] for check in publisher_run.checks] == ["neutral"]


def test_a_devel_push_run_advances_the_baseline_pointers_and_looks_up_no_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    candidate, _ = _publish_one_visual_target(monkeypatch, tmp_path)
    pointers: list[list[str]] = []
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.write_baseline_pointers", lambda slugs, **_kwargs: pointers.append(slugs)
    )
    _forbid(monkeypatch, "find_reviewable_pull_request")
    monkeypatch.setattr("sys.argv", _publisher_argv(tmp_path / "work"))

    main()

    assert pointers == [[candidate.slug]]
    assert publisher_run.comments == []
    assert [check["conclusion"] for check in publisher_run.checks] == ["success"]


def test_a_pr_run_whose_commit_is_no_longer_a_pr_head_only_closes_its_check(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", lambda **_kwargs: None)
    _forbid(monkeypatch, "download_visual_tests", "publish_pull_request_comment", "write_baseline_pointers")

    main()

    assert [(check["conclusion"], check["external_id"]) for check in publisher_run.checks] == [("neutral", PR_CHECK_ID)]
    assert publisher_run.comments == []


def test_a_failed_pr_lookup_fails_the_check_instead_of_leaving_it_in_progress(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    def ambiguous(**_kwargs: str) -> NoReturn:
        raise ValueError("Expected exactly one item in iterable, but got two")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", ambiguous)
    _forbid(monkeypatch, "download_visual_tests", "write_baseline_pointers")

    with pytest.raises(ValueError, match="Expected exactly one"):
        main()

    assert [(check["conclusion"], check["external_id"]) for check in publisher_run.checks] == [("failure", PR_CHECK_ID)]
    assert publisher_run.comments == [], "with no PR identified there is nowhere to comment"


def test_pr_recheck_failure_preserves_processing_error_and_completes_check(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    lookups = 0

    def find(**_kwargs: str) -> PullRequestRef:
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            raise LookupError("PR recheck failed")
        return PullRequestRef(number=8733, base_sha="c" * 40)

    def fail_download(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("visual collection failed")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", find)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", fail_download)

    with pytest.raises(RuntimeError, match="visual collection failed") as raised:
        main()

    assert lookups == 2
    assert isinstance(raised.value.__cause__, ExceptionGroup)
    assert [type(error) for error in raised.value.__cause__.exceptions] == [LookupError]
    assert publisher_run.comments == []
    assert [check["conclusion"] for check in publisher_run.checks] == ["failure"]


def test_comment_failure_marks_final_check_failed_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    _publish_one_visual_target(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.find_reviewable_pull_request",
        lambda **_kwargs: PullRequestRef(number=8733, base_sha="c" * 40),
    )

    def fail_comment(**_kwargs: object) -> NoReturn:
        raise OSError("comment write failed")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.publish_pull_request_comment", fail_comment)

    with pytest.raises(ExceptionGroup) as raised:
        main()

    assert [type(error) for error in raised.value.exceptions] == [OSError]
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "failure"]
    assert "reporting failed" in str(publisher_run.checks[-1]["summary"])


def test_diff_check_failure_still_comments_and_completes_final_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    _publish_one_visual_target(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.find_reviewable_pull_request",
        lambda **_kwargs: PullRequestRef(number=8733, base_sha="c" * 40),
    )

    def update_check(**kwargs: object) -> None:
        if kwargs.get("name") == "PR visual diffs":
            raise ConnectionError("diff check write failed")
        publisher_run.checks.append(kwargs)

    monkeypatch.setattr("devinfra.pr_visuals.publisher.upsert_check_run", update_check)

    with pytest.raises(ExceptionGroup) as raised:
        main()

    assert [type(error) for error in raised.value.exceptions] == [ConnectionError]
    assert [comment["pull_request"] for comment in publisher_run.comments] == [8733]
    assert [check["conclusion"] for check in publisher_run.checks] == ["failure"]


def test_processing_error_survives_comment_and_check_failures(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    attempts: list[str] = []
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.find_reviewable_pull_request",
        lambda **_kwargs: PullRequestRef(number=8733, base_sha="c" * 40),
    )

    def fail_download(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("visual collection failed")

    def fail_comment(**_kwargs: object) -> NoReturn:
        attempts.append("comment")
        raise OSError("comment write failed")

    def fail_check(**_kwargs: object) -> NoReturn:
        attempts.append("check")
        raise ConnectionError("check write failed")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", fail_download)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.publish_pull_request_comment", fail_comment)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upsert_check_run", fail_check)

    with pytest.raises(RuntimeError, match="visual collection failed") as raised:
        main()

    assert attempts == ["comment", "check"]
    assert isinstance(raised.value.__cause__, ExceptionGroup)
    assert [type(error) for error in raised.value.__cause__.exceptions] == [OSError, ConnectionError]


class PublishedObjects:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.events: list[str] = []

    def head_object(self, **kwargs: str) -> dict[str, int]:
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[key])}

    def put_object(self, **kwargs: Any) -> None:
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["ContentType"] == "image/png"
        assert kwargs["CacheControl"].endswith("immutable")
        key = kwargs["Key"]
        assert key not in self.objects
        self.puts.append(key)
        self.objects[key] = kwargs["Body"]
        self.events.append(key)

    def upload_file(self, path: Path, bucket: str, key: str, **kwargs: dict[str, str]) -> None:
        assert kwargs["ExtraArgs"]["CacheControl"].endswith("immutable")
        self.objects[key] = path.read_bytes()
        self.events.append(key)


def test_upload_reuses_shared_images_and_publishes_indexes_last(tmp_path: Path) -> None:
    source = _single_asset_test(tmp_path / "source")
    client = PublishedObjects()
    for sha in ("a" * 40, "b" * 40):
        bundle = build_bundle([source], tmp_path / "site", commit_sha=sha, repository="r")
        client.events.clear()
        upload_bundle(
            bundle, endpoint="https://s3.test", bucket="visuals", key=f"reviews-test-outputs/{sha}", client=client
        )
        if sha == "a" * 40:
            assert client.events[0].startswith("assets/")
        first_index = next(index for index, key in enumerate(client.events) if key.endswith("index.html"))
        assert all(not key.endswith("index.html") for key in client.events[:first_index])
        assert client.events[-1] == f"reviews-test-outputs/{sha}/index.html"
    assert len(client.puts) == 1
    assert client.objects[client.puts[0]] == source.assets[0].variants[0].path.read_bytes()
    assert not any(key.startswith("commits/") for key in client.objects)


@pytest.mark.parametrize(
    ("stage", "code"), [("head", "AccessDenied"), ("put", "InternalError"), ("put", "PreconditionFailed")]
)
def test_shared_image_upload_errors_do_not_publish_broken_reviews(tmp_path: Path, stage: str, code: str) -> None:
    source = _single_asset_test(tmp_path / "source")
    bundle = build_bundle([source], tmp_path / "site", commit_sha="a" * 40, repository="r")

    class RacingOrFailingS3(PublishedObjects):
        def head_object(self, **kwargs: str) -> dict[str, int]:
            if stage == "head":
                raise ClientError({"Error": {"Code": code}}, "HeadObject")
            return super().head_object(**kwargs)

        def put_object(self, **kwargs: Any) -> None:
            if code == "PreconditionFailed":
                self.objects[kwargs["Key"]] = kwargs["Body"]
            raise ClientError({"Error": {"Code": code}}, "PutObject")

    client = RacingOrFailingS3()
    if code == "PreconditionFailed":
        upload_bundle(
            bundle, endpoint="https://s3.test", bucket="visuals", key="reviews-test-outputs/sha", client=client
        )
        assert client.events[-1] == "reviews-test-outputs/sha/index.html"
    else:
        with pytest.raises(ClientError):
            upload_bundle(
                bundle, endpoint="https://s3.test", bucket="visuals", key="reviews-test-outputs/sha", client=client
            )
        assert not client.events


def test_build_bundle_classifies_structured_assets_against_eligible_baseline(tmp_path: Path) -> None:
    base_sha = "f" * 40
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    candidate = _test_from_files(
        "//ex:visuals",
        "Ex",
        [
            _png(candidate_dir / "same.png"),
            _png(candidate_dir / "changed.png", (40, 50, 60, 255)),
            _png(candidate_dir / "added.png", (1, 2, 3, 255)),
        ],
    )
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline_assets = [
        _asset_from_file(_png(baseline_dir / "same.png")),
        _asset_from_file(_png(baseline_dir / "changed.png", (70, 80, 90, 255))),
        _asset_from_file(_png(baseline_dir / "gone.png", (5, 6, 7, 255))),
    ]
    baseline_records = [
        ReviewAsset(
            path=asset.path,
            label=asset.label,
            case_id=asset.case_id,
            checkpoint=asset.checkpoint,
            role=asset.role,
            candidate_sha256=asset.variants[0].sha256,
        )
        for asset in baseline_assets
    ]
    objects = {
        f"reviews-test-outputs/{base_sha}/tests/{candidate.slug}/metadata.json": BaselineMetadata(
            assets=baseline_records,
            baseline_eligible=True,
            comparison_profile="bazel-configuration-exact-v1",
            configuration="cfg",
        )
        .model_dump_json()
        .encode(),
        **{f"assets/{asset.variants[0].sha256}.png": asset.variants[0].path.read_bytes() for asset in baseline_assets},
    }
    bundle = build_bundle(
        [candidate],
        tmp_path / "site",
        commit_sha="a" * 40,
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource(objects),
    )
    metadata = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text())
    by_label = {asset.label: asset for asset in metadata.tests[0].assets}
    assert {label: asset.classification for label, asset in by_label.items()} == {
        "same": "unchanged",
        "changed": "modified",
        "added": "new",
        "gone": "removed",
    }
    assert metadata.tests[0].summary == ClassificationCounts(modified=1, new=1, removed=1, unchanged=1)
    assert metadata.tests[0].baseline_eligible
    assert by_label["changed"].diff_sha256 is not None
    assert by_label["gone"].candidate_sha256 is None
    assert not list(bundle.rglob("*.png"))


def test_exact_candidate_digest_skips_baseline_payload_read(tmp_path: Path) -> None:
    candidate = _single_asset_test(tmp_path / "candidate")
    asset = candidate.assets[0]
    digest = asset.variants[0].sha256
    base_sha = "f" * 40
    metadata = BaselineMetadata(
        assets=[ReviewAsset(path=asset.path, label=asset.label, case_id=asset.case_id, candidate_sha256=digest)],
        baseline_eligible=True,
        comparison_profile="bazel-configuration-exact-v1",
        configuration="cfg",
    )

    class Source:
        def __init__(self) -> None:
            self.fetches: list[str] = []

        def fetch(self, key: str) -> bytes | None:
            self.fetches.append(key)
            if key == f"reviews-test-outputs/{base_sha}/tests/{candidate.slug}/metadata.json":
                return metadata.model_dump_json().encode()
            return None

    source = Source()
    bundle = build_bundle(
        [candidate], tmp_path / "site", commit_sha="a" * 40, repository="r", base_sha=base_sha, baseline_source=source
    )
    review = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text())
    assert review.tests[0].assets[0].classification == "unchanged"
    assert f"assets/{digest}.png" not in source.fetches


def test_configuration_mismatch_and_incomplete_result_cannot_advance_baseline(tmp_path: Path) -> None:
    candidate = _single_asset_test(tmp_path / "candidate")
    base_sha = "f" * 40
    asset = candidate.assets[0]
    metadata = BaselineMetadata(
        assets=[
            ReviewAsset(
                path=asset.path, label=asset.label, case_id=asset.case_id, candidate_sha256=asset.variants[0].sha256
            )
        ],
        baseline_eligible=True,
        comparison_profile="bazel-configuration-exact-v1",
        configuration="other",
    )
    objects = {
        f"reviews-test-outputs/{base_sha}/tests/{candidate.slug}/metadata.json": metadata.model_dump_json().encode()
    }
    bundle = build_bundle(
        [candidate],
        tmp_path / "site",
        commit_sha="a" * 40,
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource(objects),
    )
    review = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests[0]
    assert not review.baseline_eligible
    assert review.assets[0].classification is None
    assert any("configurations cannot be compared" in issue.message for issue in review.issues)
    incomplete = DownloadedVisualTest(
        candidate.target_label,
        candidate.slug,
        candidate.title,
        candidate.assets,
        candidate.configuration,
        [ReviewIssue(message="incomplete result coverage")],
    )
    bundle = build_bundle([incomplete], tmp_path / "site", commit_sha="b" * 40, repository="r")
    review = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests[0]
    assert not review.baseline_eligible
    assert any(issue.message == "incomplete result coverage" for issue in review.issues)


def test_unstable_variants_keep_all_images_and_disqualify_baseline(tmp_path: Path) -> None:
    source = tmp_path / "variants"
    source.mkdir()
    first = _asset_from_file(_png(source / "first.png"), case_id="ui/test_visual.py::test_screen")
    second = _asset_from_file(_png(source / "second.png", (90, 80, 70, 255)), case_id=first.case_id)
    unstable = DownloadedAsset(first.case_id, None, "screenshot", "screen", [first.variants[0], second.variants[0]])
    candidate = DownloadedVisualTest(
        "//ex:visuals", target_slug("//ex:visuals", repository="r"), "Ex", [unstable], "cfg", []
    )
    bundle = build_bundle(
        [candidate],
        tmp_path / "site",
        commit_sha="a" * 40,
        repository="r",
        base_sha="f" * 40,
        baseline_source=FakeBaselineSource({}),
    )
    review = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests[0]
    assert review.assets[0].classification == "unstable"
    assert len(review.assets[0].variants) == 2
    assert review.assets[0].candidate_sha256 is None
    assert not review.baseline_eligible
    assert review.summary is not None
    assert review.summary.unstable == 1
    for variant in review.assets[0].variants:
        assert (tmp_path / "site" / "assets" / f"{variant.sha256}.png").is_file()


def test_upload_image_checks_are_bounded_and_drained_before_review_uploads(tmp_path: Path) -> None:
    source_dir = tmp_path / "candidate"
    source_dir.mkdir()
    paths = [
        _png(source_dir / f"screen-{index}.png", (index, 20, 30, 255)) for index in range(S3_IMAGE_UPLOAD_WORKERS + 4)
    ]
    test = _test_from_files("//ex:visuals", "Ex", paths)
    bundle = build_bundle([test], tmp_path / "site", commit_sha="a" * 40, repository="r")

    class BlockingS3(PublishedObjects):
        def __init__(self) -> None:
            super().__init__()
            self.all_workers_started = Event()
            self.release = Event()
            self.lock = Lock()
            self.active = 0
            self.max_active = 0
            self.head_calls: list[str] = []
            self.review_uploads: list[str] = []

        def head_object(self, **kwargs: str) -> dict[str, int]:
            key = kwargs["Key"]
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.head_calls.append(key)
                if self.active >= S3_IMAGE_UPLOAD_WORKERS:
                    self.all_workers_started.set()
            try:
                if not self.release.wait(timeout=5):
                    raise TimeoutError("test did not release blocked image checks")
            finally:
                with self.lock:
                    self.active -= 1
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

        def upload_file(self, path: Path, bucket: str, key: str, **kwargs: dict[str, str]) -> None:
            self.review_uploads.append(key)
            super().upload_file(path, bucket, key, **kwargs)

    client = BlockingS3()
    failures: list[Exception] = []

    def publish() -> None:
        try:
            upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="reviews/a", client=client)
        except Exception as error:
            failures.append(error)

    publisher_thread = Thread(target=publish)
    publisher_thread.start()
    try:
        assert client.all_workers_started.wait(timeout=5), "all configured image workers did not start"
        with client.lock:
            assert client.active == S3_IMAGE_UPLOAD_WORKERS
            assert client.max_active == S3_IMAGE_UPLOAD_WORKERS
        assert client.events == []
        assert client.review_uploads == []
    finally:
        client.release.set()
    publisher_thread.join(timeout=10)

    assert not publisher_thread.is_alive()
    assert failures == []
    image_keys = {
        f"assets/{asset.candidate_sha256}.png"
        for review_test in ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests
        for asset in review_test.assets
        if asset.candidate_sha256 is not None
    }
    assert len(image_keys) > S3_IMAGE_UPLOAD_WORKERS
    assert len(client.head_calls) == len(image_keys)
    assert client.max_active == S3_IMAGE_UPLOAD_WORKERS
    assert set(client.events[: len(image_keys)]) == image_keys
    assert all(key.startswith("reviews/a/") for key in client.events[len(image_keys) :])
    assert client.events[-1] == "reviews/a/index.html"


def test_upload_bundle_waits_for_all_image_checks_before_propagating_failure(tmp_path: Path) -> None:
    source_dir = tmp_path / "candidate"
    source_dir.mkdir()
    paths = [_png(source_dir / f"screen-{index}.png", (index, 20, 30, 255)) for index in range(4)]
    test = _test_from_files("//ex:visuals", "Ex", paths)
    bundle = build_bundle([test], tmp_path / "site", commit_sha="a" * 40, repository="r")
    metadata = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text())
    image_keys = {
        f"assets/{asset.candidate_sha256}.png"
        for review_test in metadata.tests
        for asset in review_test.assets
        if asset.candidate_sha256 is not None
    }
    failed_key = min(image_keys)

    class OneFailureS3(PublishedObjects):
        def __init__(self) -> None:
            super().__init__()
            self.head_calls: list[str] = []

        def head_object(self, **kwargs: str) -> dict[str, int]:
            key = kwargs["Key"]
            self.head_calls.append(key)
            if key == failed_key:
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
            return {"ContentLength": 1}

    client = OneFailureS3()
    with pytest.raises(ClientError, match="HeadObject"):
        upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="reviews/a", client=client)

    assert set(client.head_calls) == image_keys
    assert client.events == []


def _asset_from_file(
    path: Path, *, case_id: str | None = None, checkpoint: str | None = None, label: str | None = None
) -> DownloadedAsset:
    body = path.read_bytes()
    return DownloadedAsset(
        case_id or f"publisher/test_publisher.py::{path.name}",
        checkpoint,
        "screenshot",
        label or path.stem,
        [
            DownloadedVariant(
                path,
                hashlib.sha256(body).hexdigest(),
                [ReviewResult(invocation="test", configuration="config", run=1, shard=1, attempt=1, status="PASSED")],
            )
        ],
    )


def _test_from_files(
    label: str,
    title: str,
    paths: list[Path],
    *,
    repository: str = "r",
    configuration: str | None = "cfg",
    issues: list[ReviewIssue] | None = None,
) -> DownloadedVisualTest:
    return DownloadedVisualTest(
        label,
        target_slug(label, repository=repository),
        title,
        [_asset_from_file(path) for path in paths],
        configuration,
        issues or [],
    )


def _single_asset_test(directory: Path, label: str = "//ex:visuals", *, repository: str = "r") -> DownloadedVisualTest:
    directory.mkdir(parents=True, exist_ok=True)
    image = _png(directory / "screen.png", (10, 20, 30, 255))
    return _test_from_files(label, "Ex", [image], repository=repository)


@pytest.mark.parametrize("pointer_fallback", [False, True])
def test_shared_images_are_reused_across_targets_and_new_baselines(tmp_path: Path, pointer_fallback: bool) -> None:
    site = tmp_path / "site"
    first = _single_asset_test(tmp_path / "first", label="//a:visual")
    second = _single_asset_test(tmp_path / "second", label="//b:visual")
    base_sha = "a" * 40
    base = build_bundle([first, second], site, commit_sha=base_sha, repository="r")
    metadata = ReviewBundleMetadata.model_validate_json((base / "metadata.json").read_text())
    digest = metadata.tests[0].assets[0].candidate_sha256
    assert digest == metadata.tests[1].assets[0].candidate_sha256
    assert len(list((site / "assets").glob("*.png"))) == 1
    objects = {path.relative_to(site).as_posix(): path.read_bytes() for path in site.rglob("*") if path.is_file()}
    if pointer_fallback:
        for test in (first, second):
            objects[f"baselines/reviews-test-outputs/{test.slug}.json"] = (
                BaselinePointer(commit_sha=base_sha).model_dump_json().encode()
            )
    new_second = _single_asset_test(tmp_path / "second-new", label="//b:visual")
    changed_path = new_second.assets[0].variants[0].path
    _png(changed_path, (20, 30, 40, 255))
    new_second = _test_from_files("//b:visual", "Ex", [changed_path])
    head = build_bundle(
        [first, new_second],
        site,
        commit_sha="b" * 40,
        repository="r",
        base_sha="c" * 40 if pointer_fallback else base_sha,
        baseline_source=FakeBaselineSource(objects),
    )
    review = ReviewBundleMetadata.model_validate_json((head / "metadata.json").read_text())
    assert review.tests[0].assets[0].classification == "unchanged"
    assert review.tests[0].baseline_fallback == pointer_fallback
    assert review.tests[1].assets[0].classification == "modified"
    assert review.tests[1].assets[0].baseline_sha256 == digest
    assert len(list((site / "assets").glob("*.png"))) == 3


def test_build_bundle_falls_back_to_eligible_devel_pointer(tmp_path: Path) -> None:
    candidate = _single_asset_test(tmp_path / "candidate")
    base_sha = "f" * 40
    pointer_sha = "a" * 40
    baseline_path = _png(tmp_path / "baseline.png", (99, 99, 99, 255))
    baseline_digest = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    asset = candidate.assets[0]
    baseline = BaselineMetadata(
        assets=[
            ReviewAsset(path=asset.path, label=asset.label, case_id=asset.case_id, candidate_sha256=baseline_digest)
        ],
        baseline_eligible=True,
        comparison_profile="bazel-configuration-exact-v1",
        configuration="cfg",
    )
    objects = {
        f"baselines/reviews-test-outputs/{candidate.slug}.json": BaselinePointer(commit_sha=pointer_sha)
        .model_dump_json()
        .encode(),
        f"reviews-test-outputs/{pointer_sha}/tests/{candidate.slug}/metadata.json": baseline.model_dump_json().encode(),
        f"assets/{baseline_digest}.png": baseline_path.read_bytes(),
    }
    bundle = build_bundle(
        [candidate],
        tmp_path / "site",
        commit_sha="b" * 40,
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource(objects),
    )
    review = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests[0]
    assert review.base_sha == pointer_sha
    assert review.baseline_fallback
    assert review.assets[0].classification == "modified"
    assert review.assets[0].baseline_stale
    assert review.merge_base_sha == base_sha


def test_build_bundle_all_new_when_no_baseline_anywhere(tmp_path: Path) -> None:
    test = _single_asset_test(tmp_path / "candidate")
    base_sha = "fedcba9876543210fedcba9876543210fedcba98"

    bundle = build_bundle(
        [test],
        tmp_path / "site",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource({}),
    )

    metadata = json.loads((bundle / "tests" / test.slug / "metadata.json").read_text())
    assert metadata["base_sha"] == base_sha
    assert "baseline_fallback" not in metadata
    assert metadata["assets"][0]["classification"] == "new"
    assert metadata["summary"] == {"modified": 0, "new": 1, "removed": 0, "unchanged": 0, "unstable": 0}
    # No modified asset to prefer — the aggregate index page still gets a thumbnail.
    assert metadata["preview"]["classification"] == "new"


def test_write_baseline_pointers_puts_mutable_json() -> None:
    class FakeS3:
        def __init__(self) -> None:
            self.puts: list[dict[str, object]] = []

        def put_object(self, **kwargs: object) -> None:
            self.puts.append(kwargs)

    client = FakeS3()
    sha = "0123456789abcdef0123456789abcdef01234567"
    write_baseline_pointers(["slug-a", "slug-b"], commit_sha=sha, bucket="visuals", client=client)

    assert [put["Key"] for put in client.puts] == [
        "baselines/reviews-test-outputs/slug-a.json",
        "baselines/reviews-test-outputs/slug-b.json",
    ]
    for put in client.puts:
        assert put["Bucket"] == "visuals"
        assert put["CacheControl"] == "no-cache"
        assert put["ContentType"] == "application/json"
        body = put["Body"]
        assert isinstance(body, bytes)
        assert BaselinePointer.model_validate_json(body).commit_sha == sha


def test_diff_check_conclusions() -> None:
    def review_test(summary: ClassificationCounts | None) -> ReviewTest:
        return ReviewTest(target_label="//t:a", slug="s", title="T", assets=[], summary=summary)

    assert diff_check([review_test(None)]) is None

    clean = diff_check([review_test(ClassificationCounts(new=1, unchanged=3))])
    assert clean is not None
    assert clean[0] == "success"

    changed = diff_check([review_test(ClassificationCounts(modified=2))])
    assert changed is not None
    assert changed[0] == "neutral"

    removed_only = diff_check([review_test(ClassificationCounts(removed=1))])
    assert removed_only is not None
    assert removed_only[0] == "neutral"


def _comment_asset(*, path: str, label: str, **kwargs: Any) -> ReviewAsset:
    return ReviewAsset(path=path, label=label, case_id=f"ui/test_visual.py::{path}", **kwargs)


def test_success_comment_body_reports_counts_and_previews() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(
            target_label="//ex:visuals",
            slug="ex-visuals-abcdef",
            title="Ex",
            summary=ClassificationCounts(modified=2, new=1, removed=0, unchanged=1),
            assets=[
                _comment_asset(
                    path="a.png", label="a", classification="modified", changed_fraction=0.5, changed_pixels=10
                ),
                _comment_asset(
                    path="b.png", label="b", classification="modified", changed_fraction=0.2, changed_pixels=4
                ),
                _comment_asset(path="c.png", label="c", classification="new"),
                _comment_asset(path="d.png", label="d", classification="unchanged"),
            ],
        )
    ]
    body = success_comment_body(
        repository="r", commit_sha=sha, url="https://v/commits/sha/", review_tests=review_tests, base_sha="f" * 40
    )
    assert "<strong>2 modified</strong>, 1 new, 0 removed, 1 unchanged" in body
    # Each preview is a before/after/diff table row.
    assert "tests/ex-visuals-abcdef/baseline/a.png" in body
    assert "tests/ex-visuals-abcdef/a.png" in body
    assert "tests/ex-visuals-abcdef/diff/a.png" in body
    assert "tests/ex-visuals-abcdef/diff/b.png" in body
    assert "tests/ex-visuals-abcdef/c.png" in body
    assert "50.0% changed" in body


def test_comment_template_escapes_html_and_preserves_table_structure() -> None:
    test = ReviewTest(
        target_label="//ex:visual",
        slug="s",
        title='<script>alert("x")</script>',
        summary=ClassificationCounts(modified=1),
        issues=[ReviewIssue(message="bad <script>")],
        assets=[
            _comment_asset(path="a.png", label='<b title="x">a|b</b>', classification="modified", changed_fraction=0.5)
        ],
    )
    body = success_comment_body(
        repository="r",
        commit_sha="a" * 40,
        url="https://v/",
        review_tests=[test],
        base_sha="b" * 40,
        ci_details_url="https://example.com/run?a=1&b=2",
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&lt;b title=&#34;x&#34;&gt;a|b&lt;/b&gt;" in body
    assert 'href="https://example.com/run?a=1&amp;b=2"' in body
    assert "<thead><tr><th>Before</th><th>After</th><th>Diff</th></tr></thead>" in body
    row = next(line for line in body.splitlines() if line.startswith("<tbody><tr>"))
    assert row.count("a|b") == 3
    assert row.count("<td>") == 3
    assert "\n\n</details>" in body


def test_comment_budget_omits_oversized_target_section() -> None:
    test = ReviewTest(
        target_label="//ex:visual",
        slug="s",
        title="Ex",
        summary=ClassificationCounts(new=1),
        assets=[_comment_asset(path="a.png", label="x" * COMMENT_BUDGET, classification="new")],
    )
    body = success_comment_body(
        repository="r", commit_sha="a" * 40, url="https://v/", review_tests=[test], base_sha="b" * 40
    )
    assert "1 changed test section omitted for length" in body
    assert len(body) <= COMMENT_BUDGET


def test_comment_template_escapes_html_code_and_link_destinations() -> None:
    test = ReviewTest(target_label="//ex:has`tick", slug="s", title="Ex", assets=[])
    body = success_comment_body(repository="r", commit_sha="a" * 40, url="https://v/(branch)/", review_tests=[test])
    assert '<a href="https://v/(branch)/tests/s/index.html"><code>//ex:has`tick</code></a>' in body
    assert '<a href="https://v/(branch)/index.html">Open visual review</a>' in body


def test_comment_alerts_use_markdown_and_escape_html_inside_code() -> None:
    body = error_comment_body(
        repository="r",
        commit_sha="a" * 40,
        error=ValueError("bad <tag> ``` &"),
        ci_conclusion="failure",
        ci_failures=["//test:<unsafe>"],
    )
    assert "> [!WARNING]" in body
    assert "> [!CAUTION]" in body
    assert "> <li><code>//test:&lt;unsafe&gt;</code></li>" in body
    assert "<pre><code>bad &lt;tag&gt; ``` &amp;</code></pre>" in body


def test_success_comment_body_warns_when_comparison_uses_fallback_baseline() -> None:
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=[
            ReviewTest(
                target_label="//ex:visuals",
                slug="ex-visuals-abcdef",
                title="Example UI",
                base_sha="fedcba9876543210fedcba9876543210fedcba98",
                baseline_fallback=True,
                summary=ClassificationCounts(modified=1),
                assets=[],
            )
        ],
        base_sha="f" * 40,
    )

    assert "[!WARNING]" in body
    assert "<code>fedcba98</code>" in body


def test_success_comment_body_labels_each_stale_screenshot() -> None:
    def asset(path: str, *, stale: bool | None) -> ReviewAsset:
        return _comment_asset(
            path=path, label=path, classification="modified", changed_fraction=0.5, baseline_stale=stale
        )

    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=[
            ReviewTest(
                target_label="//ex:visuals",
                slug="ex-visuals-abcdef",
                title="Example UI",
                base_sha="fedcba9876543210fedcba9876543210fedcba98",
                merge_base_sha="1111111122222222333333334444444455555555",
                baseline_fallback=True,
                summary=ClassificationCounts(modified=4),
                assets=[
                    asset("a.png", stale=True),
                    asset("b.png", stale=None),
                    asset("c.png", stale=None),
                    asset("d.png", stale=None),
                ],
            )
        ],
        base_sha="1" * 40,
    )

    assert body.count("stale baseline") == 2
    assert (
        "<summary>a.png — 50.0% changed — ⚠️ stale baseline · "
        '<a href="https://v/commits/sha/tests/ex-visuals-abcdef/index.html#a.png">link</a></summary>'
    ) in body.splitlines()
    assert "<code>fedcba98</code>, not merge base <code>11111111</code>" in body
    assert '<p><strong><a href="https://v/commits/sha/tests/ex-visuals-abcdef/index.html#a.png">a.png</a>' not in body


def test_success_comment_body_hides_zero_count_buckets_per_test() -> None:
    review_tests = [
        ReviewTest(
            target_label="//a:x",
            slug="a",
            title="A",
            summary=ClassificationCounts(modified=4, new=12, removed=0, unchanged=2),
            assets=[],
        ),
        ReviewTest(
            target_label="//b:y",
            slug="b",
            title="B",
            summary=ClassificationCounts(modified=0, new=0, removed=0, unchanged=5),
            assets=[],
        ),
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
    )
    # The target disclosure omits zero-count buckets from its summary.
    lines = body.splitlines()
    assert (
        "<summary><code>//a:x</code> — 4 modified, 12 new · A · "
        '<a href="https://v/commits/sha/tests/a/index.html">link</a></summary>'
    ) in lines
    assert "<summary>1 unchanged target</summary>" in lines
    assert '<a href="https://v/commits/sha/tests/b/index.html"><code>//b:y</code></a>' in body


def test_success_comment_body_is_compact_when_every_affected_test_is_unchanged() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(target_label="//a:x", slug="a", title="A", summary=ClassificationCounts(unchanged=3), assets=[]),
        ReviewTest(target_label="//b:y", slug="b", title="B", summary=ClassificationCounts(unchanged=5), assets=[]),
    ]

    body = success_comment_body(
        repository="r", commit_sha=sha, url="https://v/commits/sha/", review_tests=review_tests, base_sha="f" * 40
    )

    assert body.startswith(COMMENT_MARKER)
    assert f"https://github.com/r/commit/{sha}" in body
    assert "https://v/commits/sha/index.html" in body
    # Compact: no per-target list (the unchanged targets would be named) and no previews.
    assert "//a:x" not in body
    assert "//b:y" not in body
    assert "<details>" not in body
    assert "<img " not in body


def test_success_comment_body_shows_new_previews_when_nothing_modified() -> None:
    """A PR that only adds new fixtures (no existing screenshot changed) must still get image
    previews in the comment, not just the text counts — this was the bug: `_with_diff_previews`
    only ever collected `modified` assets and bailed out with no previews at all otherwise."""
    review_tests = [
        ReviewTest(
            target_label="//ex:visuals",
            slug="ex-visuals-abcdef",
            title="Ex",
            summary=ClassificationCounts(modified=0, new=2, removed=0, unchanged=1),
            assets=[
                _comment_asset(path="a.png", label="a", classification="new"),
                _comment_asset(path="b.png", label="b", classification="new"),
                _comment_asset(path="c.png", label="c", classification="unchanged"),
            ],
        )
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
    )
    assert "tests/ex-visuals-abcdef/a.png" in body
    assert "tests/ex-visuals-abcdef/b.png" in body
    assert body.count("<img ") == 2


def test_success_comment_body_groups_changed_tests_and_folds_unchanged_targets() -> None:
    """Changed tests get their own disclosure and the unchanged list stays compact."""
    review_tests = [
        ReviewTest(
            target_label=f"//untouched{index}:visuals",
            slug=f"untouched-{index}",
            title=f"Untouched {index}",
            summary=ClassificationCounts(unchanged=4),
            assets=[_comment_asset(path="a.png", label="a", classification="unchanged")],
        )
        for index in range(22)
    ] + [
        ReviewTest(
            target_label="//ex:visuals",
            slug="ex-visuals-abcdef",
            title="Ex",
            summary=ClassificationCounts(new=1),
            assets=[_comment_asset(path="fresh.png", label="fresh", classification="new")],
        )
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
        ci_details_url="https://github/actions/runs/123",
    )
    lines = body.splitlines()
    assert "tests/ex-visuals-abcdef/fresh.png" in body
    summary = (
        "<summary><code>//ex:visuals</code> — 1 new · Ex · "
        '<a href="https://v/commits/sha/tests/ex-visuals-abcdef/index.html">link</a> · '
        '<a href="https://github/actions/runs/123">request rerun</a></summary>'
    )
    assert summary in lines
    assert lines.index(summary) < lines.index("<summary>22 unchanged targets</summary>")
    assert '<a href="https://v/commits/sha/tests/untouched-0/index.html"><code>//untouched0:visuals</code></a>' in body
    assert len(body) <= COMMENT_BUDGET


def test_success_comment_body_dimension_change_degrades_diff_cell() -> None:
    """No diff overlay exists when dimensions changed — the Diff cell is text."""
    review_tests = [
        ReviewTest(
            target_label="//t:a",
            slug="s",
            title="T",
            summary=ClassificationCounts(modified=1),
            assets=[
                _comment_asset(
                    path="a.png",
                    label="a",
                    classification="modified",
                    changed_fraction=0.4,
                    changed_pixels=9,
                    candidate_dimensions=[8, 10],
                    baseline_dimensions=[16, 16],
                    dimension_changed=True,
                )
            ],
        )
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
    )
    assert "baseline/a.png" in body
    assert "diff/a.png" not in body


def test_success_comment_body_uses_more_of_the_comment_budget_for_previews() -> None:
    big = "x" * 3500
    review_tests = [
        ReviewTest(
            target_label="//t:a",
            slug="s",
            title="T",
            summary=ClassificationCounts(modified=2),
            assets=[
                _comment_asset(
                    path="a.png", label=big, classification="modified", changed_fraction=0.9, changed_pixels=1
                ),
                _comment_asset(
                    path="b.png", label=big, classification="modified", changed_fraction=0.1, changed_pixels=1
                ),
            ],
        )
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
    )
    # Both changed screenshots fit under the expanded budget.
    assert body.count("<img ") == 6
    assert "baseline/a.png" in body
    assert "diff/a.png" in body
    assert "baseline/b.png" in body
    assert "diff/b.png" in body
    assert len(body) <= COMMENT_BUDGET


if __name__ == "__main__":
    pytest_bazel.main()
