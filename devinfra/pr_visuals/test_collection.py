from __future__ import annotations

import hashlib
import io
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
import pytest_bazel
from botocore.exceptions import ClientError

from devinfra.pr_visuals.artifacts import BuildBuddyArtifact, Runner
from devinfra.pr_visuals.collection import (
    ResultKey,
    SourceSummary,
    collect_materialized_outputs,
    collect_outputs,
    local_output_result,
)
from devinfra.pr_visuals.record_cache import RecordCache
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
        summary=SourceSummary(
            overall_status,
            run_count,
            shard_count,
            attempt_count,
            total_run_count if total_run_count is not None else run_count * shard_count,
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


def test_direct_protocol_collects_exact_result_and_one_record_download_wave(tmp_path: Path) -> None:
    first, blobs_a = _pair("dialog/mobile", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("dialog/desktop", "b", shard=2, shard_count=2)
    calls: list[list[str]] = []
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, blobs_a | blobs_b, calls)
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


def test_same_identity_across_shards_conflicts_even_with_same_payload_uri(tmp_path: Path) -> None:
    first, blobs_a = _pair("same", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("same", "b", shard=2, shard_count=2)
    second[1].file.uri = first[1].file.uri
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, blobs_a | blobs_b, [])
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


def test_duplicate_output_records_keep_the_record_path_in_structured_issue(tmp_path: Path) -> None:
    first, first_blobs = _pair("same", "a")
    second, second_blobs = _pair("same", "b")
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, first_blobs | second_blobs, [])
    )

    issue = next(issue for issue in collected.issues if issue.message == "duplicate output identity")
    assert not collected.complete
    assert not collected.selected
    assert issue.result == ResultKey("invocation", "//ui:visual", "config-a", 1, 1, 1)
    assert issue.output is not None
    assert issue.output.case_id == "same"
    assert issue.record == "test.outputs/test-outputs/records/b.json"


def test_same_identity_in_distinct_configurations_is_kept_separate(tmp_path: Path) -> None:
    first, blobs_a = _pair("same", "a", configuration="config-a")
    second, blobs_b = _pair("same", "b", configuration="config-b")
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert collected.complete
    assert len(collected.selected) == 2
    assert {item.result.configuration for item in collected.selected} == {"config-a", "config-b"}


def test_retries_and_runs_remain_separate_and_failed_attempt_is_not_selected(tmp_path: Path) -> None:
    failed, blobs_a = _pair("same", "a", attempt=1, status="FAILED", run_count=2, attempt_count=3, total_run_count=3)
    passed, blobs_b = _pair("same", "b", attempt=2, run_count=2, attempt_count=3, total_run_count=3)
    repeated, blobs_c = _pair("same", "c", run=2, run_count=2, attempt_count=3, total_run_count=3)
    collected = collect_outputs(
        ["invocation"],
        "agentydragon/ducktape",
        tmp_path,
        run=_run(failed + passed + repeated, blobs_a | blobs_b | blobs_c, []),
    )
    assert collected.complete
    assert len(collected.observations) == 3
    assert {(item.result.run, item.result.attempt) for item in collected.selected} == {(1, 2), (2, 1)}
    assert any("retried by attempt 2" in issue.message for issue in collected.issues)
    retry_issue = next(issue for issue in collected.issues if "retried by attempt 2" in issue.message)
    assert retry_issue.result == ResultKey("invocation", "//ui:visual", "config-a", 1, 1, 1)
    assert "ResultKey(" not in retry_issue.message
    assert [(result.result.attempt, result.retried_by) for result in collected.results if result.retried_by] == [(1, 2)]


def test_uneven_shard_retries_use_total_result_count(tmp_path: Path) -> None:
    failed, blobs_a = _pair(
        "one", "a", shard=1, attempt=1, status="FAILED", shard_count=2, attempt_count=2, total_run_count=3
    )
    passed, blobs_b = _pair("one", "b", shard=1, attempt=2, shard_count=2, attempt_count=2, total_run_count=3)
    other, blobs_c = _pair("two", "c", shard=2, shard_count=2, attempt_count=2, total_run_count=3)
    collected = collect_outputs(
        ["invocation"],
        "agentydragon/ducktape",
        tmp_path,
        run=_run(failed + passed + other, blobs_a | blobs_b | blobs_c, []),
    )
    assert collected.complete
    assert {(item.result.shard, item.result.attempt) for item in collected.selected} == {(1, 2), (2, 1)}


def test_flaky_summary_selects_passing_terminal_retry(tmp_path: Path) -> None:
    failed, blobs_a = _pair(
        "one", "a", attempt=1, status="FAILED", attempt_count=2, total_run_count=2, overall_status="FLAKY"
    )
    passed, blobs_b = _pair("one", "b", attempt=2, attempt_count=2, total_run_count=2, overall_status="FLAKY")
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(failed + passed, blobs_a | blobs_b, [])
    )
    assert collected.complete
    assert [item.result.attempt for item in collected.selected] == [2]


def test_failed_shard_keeps_successful_shard_observation_selected(tmp_path: Path) -> None:
    passed, blobs_a = _pair("one", "a", shard=1, shard_count=2, total_run_count=2, overall_status="FAILED")
    failed, blobs_b = _pair(
        "two", "b", shard=2, status="FAILED", shard_count=2, total_run_count=2, overall_status="FAILED"
    )
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(passed + failed, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert [item.key.case_id for item in collected.selected] == ["one"]
    assert len(collected.observations) == 2


def test_summary_count_exposes_missing_result_attempt(tmp_path: Path) -> None:
    artifacts, blobs = _pair("one", "a", shard_count=2, total_run_count=2)
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=_run(artifacts, blobs, []))
    assert not collected.complete
    assert not collected.selected
    assert any("listed 1 result attempts but summary reports 2" in issue.message for issue in collected.issues)


def test_unknown_terminal_status_keeps_outputs_but_does_not_select(tmp_path: Path) -> None:
    artifacts, blobs = _pair("same", "a", status=None)
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=_run(artifacts, blobs, []))
    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete


@pytest.mark.parametrize("path", ["../other.png", "/absolute.png", "files//a.png", "records/x.json"])
def test_rejects_noncanonical_or_record_payload_paths(path: str, tmp_path: Path) -> None:
    artifacts, blobs = _pair("same", "a", path="files/a.png")
    invalid = json.loads(blobs[artifacts[0].file.uri])
    invalid["path"] = path
    record = Blob(json.dumps(invalid).encode())
    blobs[record.uri] = record.contents
    artifacts[0].file.uri = record.uri
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=_run(artifacts, blobs, []))
    assert not collected.complete
    assert not collected.observations


def test_missing_payload_is_incomplete_and_cannot_borrow_another_attempt(tmp_path: Path) -> None:
    first, blobs_a = _pair(
        "same", "a", path="files/shared.png", attempt=1, status="FAILED", attempt_count=2, total_run_count=2
    )
    second, blobs_b = _pair("same", "b", path="files/shared.png", attempt=2, attempt_count=2, total_run_count=2)
    second.pop()
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert len(collected.observations) == 1
    assert not collected.selected
    assert any("absent from this exact result" in issue.message for issue in collected.issues)


@pytest.mark.parametrize("reverse_listing", [False, True])
def test_missing_payload_cannot_borrow_same_named_file_from_another_shard(
    tmp_path: Path, reverse_listing: bool
) -> None:
    first, blobs_a = _pair("first", "a", path="files/shared.png", shard=1, shard_count=2)
    second, blobs_b = _pair("second", "b", path="files/shared.png", shard=2, shard_count=2)
    second.pop()
    artifacts = first + second
    if reverse_listing:
        artifacts.reverse()

    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(artifacts, blobs_a | blobs_b, [])
    )

    assert not collected.complete
    assert [(item.key.case_id, item.result.shard) for item in collected.observations] == [("first", 1)]
    assert [(item.key.case_id, item.result.shard) for item in collected.selected] == [("first", 1)]
    assert any("absent from this exact result" in issue.message for issue in collected.issues)


def test_failed_bulk_download_preserves_partial_records_without_selecting_them(tmp_path: Path) -> None:
    first, blobs_a = _pair("one", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("two", "b", shard=2, shard_count=2)
    artifacts = first + second
    blobs = blobs_a | blobs_b

    def interrupted(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words:
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            Path(manifest[0]["destination"]).write_bytes(blobs[manifest[0]["uri"]])
            return subprocess.CompletedProcess(command, 1, "", "partial transfer")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([artifact.model_dump(by_alias=True) for artifact in artifacts]), ""
        )

    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=interrupted)
    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete
    assert all("partial transfer" in result.issues[0].message for result in collected.results)

    # A second collection into the same destination must not use the first one's record.
    def no_records(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words:
            return subprocess.CompletedProcess(command, 1, "", "no transfer")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([artifact.model_dump(by_alias=True) for artifact in artifacts]), ""
        )

    repeated = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=no_records)
    assert not repeated.observations
    assert not repeated.complete


def _direct_record(root: Path, case_id: str, *, checkpoint: str | None = None) -> None:
    """Write the documented JSON wire format directly, without a pytest adapter."""
    outputs = root / "test-outputs"
    (outputs / "records").mkdir(parents=True)
    (outputs / "files").mkdir()
    (outputs / "files" / "capture.png").write_bytes(b"local PNG payload")
    (outputs / "records" / "capture.json").write_text(
        json.dumps(
            {
                "schema": "ducktape.test-output.v1",
                "case_id": case_id,
                "checkpoint": checkpoint,
                "role": "screenshot",
                "path": "files/capture.png",
            }
        )
    )


def test_direct_non_pytest_record_uses_same_collection_and_digest_rules(tmp_path: Path) -> None:
    root = tmp_path / "direct"
    _direct_record(root, "dialog/mobile", checkpoint="expanded")
    result = ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, 1)
    summary = SourceSummary("PASSED", 1, 1, 1, 1)
    collected = collect_materialized_outputs(
        [local_output_result(root, result=result, status="PASSED", summary=summary)], "agentydragon/ducktape"
    )
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
def test_materialized_source_rejects_untrusted_payload_digest(tmp_path: Path, digest: str, size: int | bool) -> None:
    root = tmp_path / "direct"
    _direct_record(root, "dialog/mobile")
    source = local_output_result(
        root,
        result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, 1),
        status="PASSED",
        summary=SourceSummary("PASSED", 1, 1, 1, 1),
    )
    files = tuple(
        replace(file, digest=digest, size=size) if file.name.endswith("capture.png") else file for file in source.files
    )
    collected = collect_materialized_outputs([replace(source, files=files)], "agentydragon/ducktape")
    assert not collected.complete
    assert not collected.selected
    assert any("no trusted content digest and size" in issue.message for issue in collected.issues)


def test_materialized_source_verifies_advertised_record_digest(tmp_path: Path) -> None:
    root = tmp_path / "direct"
    _direct_record(root, "dialog/mobile")
    source = local_output_result(
        root,
        result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, 1),
        status="PASSED",
        summary=SourceSummary("PASSED", 1, 1, 1, 1),
    )
    files = tuple(
        replace(file, contents=b"tampered") if file.name.endswith("capture.json") else file for file in source.files
    )
    collected = collect_materialized_outputs([replace(source, files=files)], "agentydragon/ducktape")
    assert not collected.complete
    assert not collected.selected
    assert any("does not match its advertised digest or size" in issue.message for issue in collected.issues)


def test_local_source_rejects_symlinked_payload(tmp_path: Path) -> None:
    root = tmp_path / "direct"
    _direct_record(root, "dialog/mobile")
    payload = root / "test-outputs" / "files" / "capture.png"
    payload.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    payload.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        local_output_result(
            root,
            result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, 1),
            status="PASSED",
            summary=SourceSummary("PASSED", 1, 1, 1, 1),
        )


def test_local_source_rejects_symlinked_root(tmp_path: Path) -> None:
    root = tmp_path / "direct"
    _direct_record(root, "dialog/mobile")
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        local_output_result(
            linked_root,
            result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, 1),
            status="PASSED",
            summary=SourceSummary("PASSED", 1, 1, 1, 1),
        )


def test_local_results_share_retry_shard_and_failed_coverage_rules(tmp_path: Path) -> None:
    summary = SourceSummary("FLAKY", 1, 2, 2, 3)
    specs = [
        ("failed", "first", 1, 1, "FAILED"),
        ("retry", "first", 1, 2, "PASSED"),
        ("other", "second", 2, 1, "PASSED"),
    ]
    sources = []
    for folder, case_id, shard, attempt, status in specs:
        root = tmp_path / folder
        _direct_record(root, case_id)
        sources.append(
            local_output_result(
                root,
                result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, shard, attempt),
                status=status,
                summary=summary,
            )
        )
    collected = collect_materialized_outputs(sources, "agentydragon/ducktape")
    assert collected.complete
    assert {(item.result.shard, item.result.attempt) for item in collected.selected} == {(1, 2), (2, 1)}

    failed_other = local_output_result(
        tmp_path / "other",
        result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, 2, 1),
        status="FAILED",
        summary=SourceSummary("FAILED", 1, 2, 2, 3),
    )
    failed_first = [
        local_output_result(
            tmp_path / folder,
            result=ResultKey("local-invocation", "//ui:visual", "config-a", 1, 1, attempt),
            status=status,
            summary=SourceSummary("FAILED", 1, 2, 2, 3),
        )
        for folder, _, _, attempt, status in specs[:2]
    ]
    partial = collect_materialized_outputs([*failed_first, failed_other], "agentydragon/ducktape")
    assert not partial.complete
    assert [(item.result.shard, item.result.attempt) for item in partial.selected] == [(1, 2)]


class _CacheS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.read_error: str | None = None

    def get_object(self, **kwargs: str) -> dict[str, io.BytesIO]:
        if self.read_error:
            raise ClientError({"Error": {"Code": self.read_error}}, "GetObject")
        try:
            return {"Body": io.BytesIO(self.objects[kwargs["Bucket"], kwargs["Key"]])}
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from None

    def put_object(self, **kwargs: object) -> None:
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[str(kwargs["Bucket"]), str(kwargs["Key"])] = body


def test_cache_reuse_keeps_fresh_result_status_and_payload_validation(tmp_path: Path) -> None:
    artifacts, blobs = _pair("dialog", "same")
    s3 = _CacheS3()
    first_calls: list[list[str]] = []
    first = collect_outputs(
        ["old"],
        "owner/repo",
        tmp_path / "old",
        run=_run(artifacts, blobs, first_calls),
        record_cache=RecordCache(s3, "private-records", "owner/repo", writable=True),
    )
    assert first.complete
    assert len([call for call in first_calls if "blob" in call]) == 1

    fresh_calls: list[list[str]] = []
    failed_artifacts = [
        _artifact(item.file.name, item.file.uri, status="FAILED", overall_status="FAILED") for item in artifacts
    ]
    fresh = collect_outputs(
        ["new"],
        "owner/repo",
        tmp_path / "new",
        run=_run(failed_artifacts, blobs, fresh_calls),
        record_cache=RecordCache(s3, "private-records", "owner/repo"),
    )
    assert not fresh.selected
    assert fresh.results[0].result.invocation == "new"
    assert fresh.results[0].status == "FAILED"
    assert any("artifact" in call for call in fresh_calls)
    assert not any("blob" in call for call in fresh_calls)

    missing_payload = collect_outputs(
        ["new"],
        "owner/repo",
        tmp_path / "missing",
        run=_run(failed_artifacts[:1], blobs, []),
        record_cache=RecordCache(s3, "private-records", "owner/repo"),
    )
    assert not missing_payload.complete
    assert any("absent from this exact result" in issue.message for issue in missing_payload.issues)


def test_duplicate_cas_record_downloaded_once_and_reused_for_distinct_results(tmp_path: Path) -> None:
    first, first_blobs = _pair("shared", "a", path="files/shared.png", target="//ui:first")
    second, second_blobs = _pair("shared", "b", path="files/shared.png", target="//ui:second")
    manifests: list[list[dict[str, str]]] = []

    def inspect(command: list[str | Path], **kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words:
            manifests.append(json.loads(Path(words[words.index("--manifest") + 1]).read_text()))
        return _run(first + second, first_blobs | second_blobs, [])(command, **kwargs)

    collected = collect_outputs(["invocation"], "owner/repo", tmp_path, run=inspect)
    assert collected.complete
    assert {item.key.target for item in collected.selected} == {"//ui:first", "//ui:second"}
    assert len(manifests) == 1
    assert len(manifests[0]) == 1


def test_incomplete_devel_collection_does_not_replace_snapshot(tmp_path: Path) -> None:
    artifacts, blobs = _pair("dialog", "a")
    s3 = _CacheS3()
    incomplete = collect_outputs(
        ["invocation"],
        "owner/repo",
        tmp_path,
        run=_run(artifacts[:1], blobs, []),
        record_cache=RecordCache(s3, "private-records", "owner/repo", writable=True),
    )
    assert not incomplete.complete
    assert not s3.objects


def test_corrupt_private_cache_snapshot_redownloads_records(tmp_path: Path) -> None:
    artifacts, blobs = _pair("dialog", "a")
    s3 = _CacheS3()
    cache = RecordCache(s3, "private-records", "owner/repo")
    s3.objects[cache.bucket, cache.key] = b"invalid compressed cache"
    calls: list[list[str]] = []
    collected = collect_outputs(
        ["invocation"], "owner/repo", tmp_path, run=_run(artifacts, blobs, calls), record_cache=cache
    )
    assert collected.complete
    assert len([call for call in calls if "blob" in call]) == 1


def test_missing_private_cache_bucket_falls_back_to_buildbuddy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts, blobs = _pair("dialog", "a")
    s3 = _CacheS3()
    s3.read_error = "NoSuchBucket"
    calls: list[list[str]] = []
    collected = collect_outputs(
        ["invocation"],
        "owner/repo",
        tmp_path,
        run=_run(artifacts, blobs, calls),
        record_cache=RecordCache(s3, "private-records", "owner/repo", writable=True),
    )
    assert collected.complete
    assert len([call for call in calls if "blob" in call]) == 1
    assert not s3.objects
    assert "using BuildBuddy" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
