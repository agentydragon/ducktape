import json

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci import bes
from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, TestSummary, merge, read
from devinfra.buildbuddy.cli import CLIError

EXPORT = b"""{
  "outputs": [{
    "id": {"targetCompleted": {"label": "//pkg:wheel"}},
    "file": {
      "pathPrefix": ["bazel-out", "k8-fastbuild", "bin"],
      "name": "pkg/wheel.whl",
      "uri": "bytestream://host/blobs/abc/12",
      "digest": "abc",
      "length": "12"
    },
    "outputGroup": "default"
  }],
  "testStatus": {"//pkg:test": "PASSED"},
  "testSummaries": [
    {"id": {"label": "//pkg:test", "configuration": {"id": "cfg"}},
     "summary": {"overallStatus": "PASSED", "attemptCount": 0, "totalNumCached": 0},
     "attemptCountPresent": true, "cachedCountPresent": true},
    {"id": {"label": "//pkg:test", "configuration": {"id": "other"}},
     "summary": {"overallStatus": "PASSED", "attemptCount": 0, "totalNumCached": 0},
     "attemptCountPresent": false, "cachedCountPresent": false}
  ],
  "finished": true,
  "successful": true,
  "command": "test",
  "requestedPatterns": {"values": ["//pkg:..."]},
  "explicitCmdLine": {"values": ["test", "//pkg:..."]},
  "commitSha": "deadbeef"
}"""


def test_read_adapts_structured_export_and_preserves_present_zero_counts(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> bytes:
        calls.append(args)
        return EXPORT

    monkeypatch.setattr(bes, "run", fake_run)
    invocation = read("inv-id")

    assert calls == [("invocation", "export", "inv-id")]
    assert invocation.outputs == [
        Output(
            label="//pkg:wheel",
            path="bazel-out/k8-fastbuild/bin/pkg/wheel.whl",
            uri="bytestream://host/blobs/abc/12",
            digest="abc",
            output_group="default",
            aspect="",
        )
    ]
    assert invocation.test_summaries == [
        TestSummary("//pkg:test", "cfg", "PASSED", 0, 0),
        TestSummary("//pkg:test", "other", "PASSED", None, None),
    ]
    assert (invocation.finished, invocation.successful) == (True, True)
    assert invocation.command == "test"
    assert invocation.requested_patterns == ("//pkg:...",)
    assert invocation.explicit_cmd_line == ("test", "//pkg:...")
    assert invocation.commit_sha == "deadbeef"


def test_read_wraps_cli_errors(monkeypatch) -> None:
    def fail(*_args: str, **_kwargs) -> bytes:
        raise CLIError("bbapi invocation export failed")

    monkeypatch.setattr(bes, "run", fail)
    with pytest.raises(BuildBuddyError, match="bbapi invocation export failed"):
        read("missing")


def test_read_rejects_invalid_export(monkeypatch) -> None:
    monkeypatch.setattr(bes, "run", lambda *_args, **_kwargs: b'{"outputs": []}')
    with pytest.raises(BuildBuddyError, match="invalid JSON"):
        read("inv-id")


def test_read_preserves_runner_lineage_and_attempt_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.loads(EXPORT)
    document.update(
        invocationId="retry",
        parentInvocationId="runner",
        childInvocationIds=["first", "second"],
        exitCodeName="SUCCESS",
    )
    monkeypatch.setattr(bes, "run", lambda *_args, **_kwargs: json.dumps(document).encode())
    invocation = read("retry")
    assert invocation.invocation_id == "retry"
    assert invocation.parent_invocation_id == "runner"
    assert invocation.child_invocation_ids == ("first", "second")
    assert invocation.exit_code_name == "SUCCESS"


def test_read_rejects_a_stream_from_another_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.loads(EXPORT)
    document["invocationId"] = "another-invocation"
    monkeypatch.setattr(bes, "run", lambda *_args, **_kwargs: json.dumps(document).encode())
    with pytest.raises(BuildBuddyError, match="requested invocation expected, received another-invocation"):
        read("expected")


def test_fetch_blob_uses_bbapi_blob_cat(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> bytes:
        calls.append(args)
        return b"blob"

    monkeypatch.setattr(bes, "run", fake_run)
    assert bes.fetch_blob("bytestream://host/blobs/abc/12") == b"blob"
    assert calls == [("blob", "cat", "bytestream://host/blobs/abc/12")]


def test_fetch_blob_wraps_cli_errors(monkeypatch) -> None:
    def fail(*_args: str, **_kwargs) -> bytes:
        raise CLIError("blob read failed")

    monkeypatch.setattr(bes, "run", fail)
    with pytest.raises(BuildBuddyError, match="blob read failed"):
        bes.fetch_blob("bytestream://host/blobs/abc/12")


def test_merge_deduplicates_shared_outputs() -> None:
    output = Output(label="//p:t", path="bazel-out/bin/p/t.whl", uri="u", digest="d", output_group="default")
    merged = merge([Invocation(outputs=[output]), Invocation(outputs=[output])])
    assert merged is not None
    assert merged.outputs == [output]
    assert merge([]) is None


if __name__ == "__main__":
    pytest_bazel.main()
