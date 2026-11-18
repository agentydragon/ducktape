from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, Unpack

import pytest

from devinfra.pr_visuals.artifacts import BESTestSummary, BuildBuddyArtifact, Runner
from devinfra.pr_visuals.collection import (
    ResultKey,
    SourceFile,
    SourceResult,
    collect_materialized_outputs,
    collect_outputs,
)
from devinfra.pr_visuals.testing.outputs import Blob, result_artifact
from devinfra.testing.output_records import SCHEMA, OutputRecord


def _artifact(
    name: str,
    uri: str,
    *,
    target: str = "//ui:visual",
    configuration: str = "config-a",
    run: int = 1,
    shard: int = 1,
    attempt: int = 1,
    status: str | None = "PASSED",
    run_count: int = 1,
    shard_count: int = 1,
    attempt_count: int = 1,
    total_run_count: int | None = None,
    overall_status: str = "PASSED",
) -> BuildBuddyArtifact:
    return result_artifact(
        name,
        uri,
        result=ResultKey("invocation", target, configuration, run, shard, attempt),
        summary=BESTestSummary(
            overallStatus=overall_status,
            runCount=run_count,
            shardCount=shard_count,
            attemptCount=attempt_count,
            totalRunCount=total_run_count if total_run_count is not None else run_count * shard_count,
        ),
        status=status,
    )


def _record(case: str, path: str, *, checkpoint: str | None = None, role: str = "screenshot") -> bytes:
    return (
        OutputRecord(schema_=SCHEMA, case_id=case, checkpoint=checkpoint, role=role, path=path, label="Readable name")
        .model_dump_json(by_alias=True)
        .encode()
    )


class ResultOptions(TypedDict, total=False):
    target: str
    configuration: str
    run: int
    shard: int
    attempt: int
    status: str | None
    run_count: int
    shard_count: int
    attempt_count: int
    total_run_count: int | None
    overall_status: str


def _pair(
    case: str, tag: str, *, path: str | None = None, **identity: Unpack[ResultOptions]
) -> tuple[list[BuildBuddyArtifact], dict[str, bytes]]:
    path = path or f"files/{tag}.png"
    record_bytes = _record(case, path)
    record = Blob(record_bytes)
    payload = Blob(f"payload-{tag}".encode())
    return (
        [
            _artifact(f"test.outputs/test-outputs/records/{tag}.json", record.uri, **identity),
            _artifact(f"test.outputs/test-outputs/{path}", payload.uri, **identity),
        ],
        {record.uri: record.contents, payload.uri: payload.contents},
    )


def _run(artifacts: list[BuildBuddyArtifact], blobs: dict[str, bytes], calls: list[list[str]]) -> Runner:
    def fake(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        calls.append(words)
        if "blob" in words:
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            for item in manifest:
                Path(item["destination"]).write_bytes(blobs[item["uri"]])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([artifact.model_dump(by_alias=True) for artifact in artifacts]), ""
        )

    return fake


def test_direct_protocol_collects_exact_result_and_one_record_download_wave() -> None:
    first, blobs_a = _pair("dialog/mobile", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("dialog/desktop", "b", shard=2, shard_count=2)
    calls: list[list[str]] = []
    collected = collect_outputs(
        ["invocation"],
        "agentydragon/ducktape",
        targets=iter(["//ui:visual"]),
        run=_run(first + second, blobs_a | blobs_b, calls),
    )
    assert collected.complete
    assert len(collected.selected) == 2
    assert {item.key.case_id for item in collected.selected} == {"dialog/mobile", "dialog/desktop"}
    assert {item.result.shard for item in collected.selected} == {1, 2}
    assert {item.digest for item in collected.selected} == {
        hashlib.sha256(f"payload-{tag}".encode()).hexdigest() for tag in ("a", "b")
    }
    assert {item.size for item in collected.selected} == {len(b"payload-a")}
    assert len([call for call in calls if "blob" in call]) == 1


def test_same_identity_across_shards_conflicts_even_with_same_payload_uri() -> None:
    first, blobs_a = _pair("same", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("same", "b", shard=2, shard_count=2)
    second[1].file.uri = first[1].file.uri
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert len(collected.observations) == 2
    assert {(item.result.shard, item.key.case_id) for item in collected.selected} == {(1, "same"), (2, "same")}
    issue = next(issue for issue in collected.issues if "claimed across shards" in issue.message)
    assert issue.result == ResultKey("invocation", "//ui:visual", "config-a", 1, 2, 1)
    assert issue.related_results == (ResultKey("invocation", "//ui:visual", "config-a", 1, 1, 1),)
    assert issue.output is not None
    assert issue.output.case_id == "same"
    assert "OutputKey(" not in issue.message


def test_duplicate_output_records_keep_the_record_path_in_structured_issue() -> None:
    first, first_blobs = _pair("same", "a")
    second, second_blobs = _pair("same", "b")
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(first + second, first_blobs | second_blobs, [])
    )

    issue = next(issue for issue in collected.issues if issue.message == "duplicate output identity")
    assert not collected.complete
    assert not collected.selected
    assert issue.result == ResultKey("invocation", "//ui:visual", "config-a", 1, 1, 1)
    assert issue.output is not None
    assert issue.output.case_id == "same"
    assert issue.record == "test.outputs/test-outputs/records/b.json"


def test_same_identity_in_distinct_configurations_is_kept_separate() -> None:
    first, blobs_a = _pair("same", "a", configuration="config-a")
    second, blobs_b = _pair("same", "b", configuration="config-b")
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert collected.complete
    assert len(collected.selected) == 2
    assert {item.result.configuration for item in collected.selected} == {"config-a", "config-b"}


def test_retries_and_runs_remain_separate_and_failed_attempt_is_not_selected() -> None:
    failed, blobs_a = _pair("same", "a", attempt=1, status="FAILED", run_count=2, attempt_count=3, total_run_count=3)
    passed, blobs_b = _pair("same", "b", attempt=2, run_count=2, attempt_count=3, total_run_count=3)
    repeated, blobs_c = _pair("same", "c", run=2, run_count=2, attempt_count=3, total_run_count=3)
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(failed + passed + repeated, blobs_a | blobs_b | blobs_c, [])
    )
    assert collected.complete
    assert len(collected.observations) == 3
    assert {(item.result.run, item.result.attempt) for item in collected.selected} == {(1, 2), (2, 1)}
    assert any("retried by attempt 2" in issue.message for issue in collected.issues)
    retry_issue = next(issue for issue in collected.issues if "retried by attempt 2" in issue.message)
    assert retry_issue.result == ResultKey("invocation", "//ui:visual", "config-a", 1, 1, 1)
    assert "ResultKey(" not in retry_issue.message
    assert [(result.result.attempt, result.retried_by) for result in collected.results if result.retried_by] == [(1, 2)]


def test_uneven_shard_retries_use_total_result_count() -> None:
    failed, blobs_a = _pair(
        "one", "a", shard=1, attempt=1, status="FAILED", shard_count=2, attempt_count=2, total_run_count=3
    )
    passed, blobs_b = _pair("one", "b", shard=1, attempt=2, shard_count=2, attempt_count=2, total_run_count=3)
    other, blobs_c = _pair("two", "c", shard=2, shard_count=2, attempt_count=2, total_run_count=3)
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(failed + passed + other, blobs_a | blobs_b | blobs_c, [])
    )
    assert collected.complete
    assert {(item.result.shard, item.result.attempt) for item in collected.selected} == {(1, 2), (2, 1)}


def test_flaky_summary_selects_passing_terminal_retry() -> None:
    failed, blobs_a = _pair(
        "one", "a", attempt=1, status="FAILED", attempt_count=2, total_run_count=2, overall_status="FLAKY"
    )
    passed, blobs_b = _pair("one", "b", attempt=2, attempt_count=2, total_run_count=2, overall_status="FLAKY")
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(failed + passed, blobs_a | blobs_b, [])
    )
    assert collected.complete
    assert [item.result.attempt for item in collected.selected] == [2]


def test_failed_shard_keeps_successful_shard_observation_selected() -> None:
    passed, blobs_a = _pair("one", "a", shard=1, shard_count=2, total_run_count=2, overall_status="FAILED")
    failed, blobs_b = _pair(
        "two", "b", shard=2, status="FAILED", shard_count=2, total_run_count=2, overall_status="FAILED"
    )
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(passed + failed, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert [item.key.case_id for item in collected.selected] == ["one"]
    assert len(collected.observations) == 2


def test_summary_count_exposes_missing_result_attempt() -> None:
    artifacts, blobs = _pair("one", "a", shard_count=2, total_run_count=2)
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=_run(artifacts, blobs, []))
    assert not collected.complete
    assert not collected.selected
    assert any("listed 1 result attempts but summary reports 2" in issue.message for issue in collected.issues)


def test_conflicting_artifact_summaries_prevent_result_selection() -> None:
    artifacts, blobs = _pair("one", "a")
    summary = artifacts[1].test_summary
    assert summary is not None
    artifacts[1].test_summary = summary.model_copy(update={"total_run_count": 2})

    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=_run(artifacts, blobs, []))

    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete
    assert any(issue.message == "missing or inconsistent test summary" for issue in collected.issues)


def test_unknown_terminal_status_keeps_outputs_but_does_not_select() -> None:
    artifacts, blobs = _pair("same", "a", status=None)
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=_run(artifacts, blobs, []))
    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete


@pytest.mark.parametrize("filename", ["subdir/a.json", "./a.json", "a\\b.json", "a\x00b.json", ".json"])
def test_rejects_invalid_record_names_before_downloading(filename: str) -> None:
    name = f"test.outputs/test-outputs/records/{filename}"
    record = _artifact(name, Blob(b"unneeded").uri)
    calls: list[list[str]] = []
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=_run([record], {}, calls))

    assert not collected.complete
    assert any(issue.message == f"invalid record artifact name {name!r}" for issue in collected.issues)
    assert all("blob" not in call for call in calls)


@pytest.mark.parametrize("path", ["../other.png", "/absolute.png", "files//a.png", "records/x.json"])
def test_rejects_noncanonical_or_record_payload_paths(path: str) -> None:
    artifacts, blobs = _pair("same", "a", path="files/a.png")
    invalid = json.loads(blobs[artifacts[0].file.uri])
    invalid["path"] = path
    record = Blob(json.dumps(invalid).encode())
    blobs[record.uri] = record.contents
    artifacts[0].file.uri = record.uri
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=_run(artifacts, blobs, []))
    assert not collected.complete
    assert not collected.observations


def test_missing_payload_is_incomplete_and_cannot_borrow_another_attempt() -> None:
    first, blobs_a = _pair(
        "same", "a", path="files/shared.png", attempt=1, status="FAILED", attempt_count=2, total_run_count=2
    )
    second, blobs_b = _pair("same", "b", path="files/shared.png", attempt=2, attempt_count=2, total_run_count=2)
    second.pop()
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert len(collected.observations) == 1
    assert not collected.selected
    assert any("absent from this exact result" in issue.message for issue in collected.issues)


@pytest.mark.parametrize("reverse_listing", [False, True])
def test_missing_payload_cannot_borrow_same_named_file_from_another_shard(reverse_listing: bool) -> None:
    first, blobs_a = _pair("first", "a", path="files/shared.png", shard=1, shard_count=2)
    second, blobs_b = _pair("second", "b", path="files/shared.png", shard=2, shard_count=2)
    second.pop()
    artifacts = first + second
    if reverse_listing:
        artifacts.reverse()

    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=_run(artifacts, blobs_a | blobs_b, []))

    assert not collected.complete
    assert [(item.key.case_id, item.result.shard) for item in collected.observations] == [("first", 1)]
    assert [(item.key.case_id, item.result.shard) for item in collected.selected] == [("first", 1)]
    assert any("absent from this exact result" in issue.message for issue in collected.issues)


def test_failed_bulk_download_preserves_partial_records_without_selecting_them() -> None:
    first, blobs_a = _pair("one", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("two", "b", shard=2, shard_count=2)
    artifacts = first + second
    blobs = blobs_a | blobs_b
    staged: list[Path] = []

    def interrupted(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words:
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            destination = Path(manifest[0]["destination"])
            destination.write_bytes(blobs[manifest[0]["uri"]])
            staged.append(destination)
            return subprocess.CompletedProcess(command, 1, "", "partial transfer")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([artifact.model_dump(by_alias=True) for artifact in artifacts]), ""
        )

    collected = collect_outputs(["invocation"], "agentydragon/ducktape", run=interrupted)
    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete
    assert all("partial transfer" in result.issues[0].message for result in collected.results)
    assert not staged[0].parent.exists()

    # A second collection must not reuse the record from the first one.
    def no_records(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words:
            return subprocess.CompletedProcess(command, 1, "", "no transfer")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([artifact.model_dump(by_alias=True) for artifact in artifacts]), ""
        )

    repeated = collect_outputs(["invocation"], "agentydragon/ducktape", run=no_records)
    assert not repeated.observations
    assert not repeated.complete


def _materialized_source() -> SourceResult:
    payload = Blob(b"local PNG payload")
    record = Blob(
        b'{"schema":"ducktape.test-output.v1","case_id":"dialog/mobile","checkpoint":"expanded",'
        b'"role":"screenshot","path":"files/capture.png"}'
    )
    return SourceResult(
        ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, 1),
        "PASSED",
        BESTestSummary(overallStatus="PASSED", runCount=1, shardCount=1, attemptCount=1, totalRunCount=1),
        (
            SourceFile(
                "test.outputs/test-outputs/files/capture.png",
                "file:///visual-results/files/capture.png",
                payload.digest,
                len(payload.contents),
            ),
            SourceFile(
                "test.outputs/test-outputs/records/capture.json",
                "file:///visual-results/records/capture.json",
                record.digest,
                len(record.contents),
                record.contents,
            ),
        ),
    )


def test_collects_handwritten_record_from_materialized_source() -> None:
    collected = collect_materialized_outputs([_materialized_source()], "agentydragon/ducktape")
    assert collected.complete
    assert len(collected.selected) == 1
    observation = collected.selected[0]
    assert (observation.key.case_id, observation.key.checkpoint, observation.key.role) == (
        "dialog/mobile",
        "expanded",
        "screenshot",
    )
    assert observation.uri.startswith("file://")
    assert observation.record_uri.startswith("file://")
    assert observation.digest == hashlib.sha256(b"local PNG payload").hexdigest()
    assert observation.size == len(b"local PNG payload")


@pytest.mark.parametrize(
    ("digest", "size"), [("A" * 64, 17), ("not-a-digest", 17), (42, 17), ("0" * 64, -1), ("0" * 64, True)]
)
def test_materialized_source_rejects_untrusted_payload_digest(digest: str, size: int | bool) -> None:
    source = _materialized_source()
    files = tuple(
        replace(file, digest=digest, size=size) if file.name.endswith("capture.png") else file for file in source.files
    )
    collected = collect_materialized_outputs([replace(source, files=files)], "agentydragon/ducktape")
    assert not collected.complete
    assert not collected.selected
    assert any("no trusted content digest and size" in issue.message for issue in collected.issues)


def test_materialized_source_verifies_advertised_record_digest() -> None:
    source = _materialized_source()
    files = tuple(
        replace(file, contents=b"tampered") if file.name.endswith("capture.json") else file for file in source.files
    )
    collected = collect_materialized_outputs([replace(source, files=files)], "agentydragon/ducktape")
    assert not collected.complete
    assert not collected.selected
    assert any("does not match its advertised digest or size" in issue.message for issue in collected.issues)
