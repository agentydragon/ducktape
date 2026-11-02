from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from devinfra.pr_visuals.collection import (
    ResultKey,
    SourceSummary,
    collect_materialized_outputs,
    collect_outputs,
    local_output_result,
)


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
) -> dict[str, Any]:
    return {
        "id": {
            "testResult": {
                "label": target,
                "configuration": {"id": configuration},
                "run": run,
                "shard": shard,
                "attempt": attempt,
            }
        },
        "file": {"name": name, "uri": uri},
        "status": status,
        "testSummary": {
            "overallStatus": overall_status,
            "runCount": run_count,
            "shardCount": shard_count,
            "attemptCount": attempt_count,
            "totalRunCount": total_run_count if total_run_count is not None else run_count * shard_count,
        },
    }


def _record(case: str, path: str, *, checkpoint: str | None = None, role: str = "screenshot") -> bytes:
    return json.dumps(
        {
            "schema": "ducktape.test-output.v1",
            "case_id": case,
            "checkpoint": checkpoint,
            "role": role,
            "path": path,
            "label": "Readable name",
        }
    ).encode()


def _pair(
    case: str, tag: str, *, path: str | None = None, **identity: Any
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    path = path or f"files/{tag}.png"
    record_bytes = _record(case, path)
    record_uri = (
        f"bytestream://remote.buildbuddy.io/blobs/{hashlib.sha256(record_bytes).hexdigest()}/{len(record_bytes)}"
    )
    payload_uri = f"bytestream://remote.buildbuddy.io/blobs/{hashlib.sha256(('payload-' + tag).encode()).hexdigest()}/2"
    return (
        [
            _artifact(f"test.outputs/test-outputs/records/{tag}.json", record_uri, **identity),
            _artifact(f"test.outputs/test-outputs/{path}", payload_uri, **identity),
        ],
        {record_uri: record_bytes},
    )


def _run(artifacts: list[dict[str, Any]], blobs: dict[str, bytes], calls: list[list[str]]) -> Any:
    def fake(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        calls.append(words)
        if "blob" in words:
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            for item in manifest:
                Path(item["destination"]).write_bytes(blobs[item["uri"]])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

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
    assert {item.size for item in collected.selected} == {2}
    assert len([call for call in calls if "blob" in call]) == 1


def test_same_identity_across_shards_conflicts_even_with_same_payload_uri(tmp_path: Path) -> None:
    first, blobs_a = _pair("same", "a", shard=1, shard_count=2)
    second, blobs_b = _pair("same", "b", shard=2, shard_count=2)
    second[1]["file"]["uri"] = first[1]["file"]["uri"]
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert len(collected.observations) == 2
    assert any("also claimed by shard" in issue for issue in collected.issues)


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
    assert any("retried by attempt 2" in issue for issue in collected.issues)


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
    assert any("listed 1 result attempts but summary reports 2" in issue for issue in collected.issues)


def test_unknown_terminal_status_keeps_outputs_but_does_not_select(tmp_path: Path) -> None:
    artifacts, blobs = _pair("same", "a", status=None)
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=_run(artifacts, blobs, []))
    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete


@pytest.mark.parametrize("path", ["../other.png", "/absolute.png", "files//a.png", "records/x.json"])
def test_rejects_noncanonical_or_record_payload_paths(path: str, tmp_path: Path) -> None:
    artifacts, blobs = _pair("same", "a", path="files/a.png")
    blobs[artifacts[0]["file"]["uri"]] = _record("same", path)
    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=_run(artifacts, blobs, []))
    assert not collected.complete
    assert not collected.observations


def test_missing_payload_is_incomplete_and_cannot_borrow_another_attempt(tmp_path: Path) -> None:
    first, blobs_a = _pair("same", "a", attempt=1, status="FAILED", attempt_count=2, total_run_count=2)
    second, blobs_b = _pair("same", "b", attempt=2, attempt_count=2, total_run_count=2)
    second.pop()
    collected = collect_outputs(
        ["invocation"], "agentydragon/ducktape", tmp_path, run=_run(first + second, blobs_a | blobs_b, [])
    )
    assert not collected.complete
    assert len(collected.observations) == 1
    assert not collected.selected
    assert any("absent from this exact result" in issue for issue in collected.issues)


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
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    collected = collect_outputs(["invocation"], "agentydragon/ducktape", tmp_path, run=interrupted)
    assert len(collected.observations) == 1
    assert not collected.selected
    assert not collected.complete
    assert all("partial transfer" in result.issues[0] for result in collected.results)

    # A second collection into the same destination must not use the first one's record.
    def no_records(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words:
            return subprocess.CompletedProcess(command, 1, "", "no transfer")
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

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
    assert any("no trusted content digest and size" in issue for issue in collected.issues)


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
    assert any("does not match its advertised digest or size" in issue for issue in collected.issues)


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


if __name__ == "__main__":
    pytest_bazel.main()
