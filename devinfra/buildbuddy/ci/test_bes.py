import pytest
import pytest_bazel

from devinfra.buildbuddy.ci import bes
from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, TestSummary, artifact_output, merge, read
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
    calls: list[tuple[tuple[str, ...], dict]] = []

    def fake_run(*args: str, **kwargs) -> bytes:
        calls.append((args, kwargs))
        return EXPORT

    monkeypatch.setattr(bes, "run", fake_run)
    invocation = read("inv-id", api_key="secret", base_url="https://bb.example", timeout=9)

    assert calls == [
        (("invocation", "export", "inv-id"), {"timeout": 9, "api_key": "secret", "base_url": "https://bb.example"})
    ]
    assert invocation.outputs == [
        Output(
            label="//pkg:wheel",
            path="bazel-out/k8-fastbuild/bin/pkg/wheel.whl",
            uri="bytestream://host/blobs/abc/12",
            digest="abc",
            size=12,
            output_group="default",
            aspect="",
        )
    ]
    assert invocation.test_status == {"//pkg:test": "PASSED"}
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


def test_fetch_blob_uses_bbapi_blob_cat_and_optional_invocation(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict]] = []

    def fake_run(*args: str, **kwargs) -> bytes:
        calls.append((args, kwargs))
        return b"blob"

    monkeypatch.setattr(bes, "run", fake_run)
    assert (
        bes.fetch_blob(
            "bytestream://host/blobs/abc/12",
            invocation_id="inv-id",
            api_key="secret",
            base_url="https://bb.example",
            timeout=4,
        )
        == b"blob"
    )
    assert calls == [
        (
            ("blob", "cat", "bytestream://host/blobs/abc/12", "--invocation", "inv-id"),
            {"timeout": 4, "api_key": "secret", "base_url": "https://bb.example"},
        )
    ]


def test_fetch_blob_wraps_cli_errors(monkeypatch) -> None:
    def fail(*_args: str, **_kwargs) -> bytes:
        raise CLIError("blob read failed")

    monkeypatch.setattr(bes, "run", fail)
    with pytest.raises(BuildBuddyError, match="blob read failed"):
        bes.fetch_blob("bytestream://host/blobs/abc/12")


def artifact(path: str, output_group: str = "default", aspect: str = "") -> Output:
    return Output(label="//p:t", path=path, uri="u", digest="d", size=1, output_group=output_group, aspect=aspect)


def test_artifact_output_resolves_only_the_targets_own_default_output() -> None:
    by_label = {
        "//p:t": [
            artifact("bazel-out/bin/p/t.mypy_stdout", output_group="mypy", aspect="lint%mypy_aspect"),
            artifact("bazel-out/bin/p/t.whl"),
            artifact("bazel-out/bin/p/t.check", output_group="_validation"),
        ]
    }
    assert artifact_output(by_label, "//p:t") == artifact("bazel-out/bin/p/t.whl")


def test_artifact_output_reports_absent_or_ambiguous_targets() -> None:
    absent = artifact_output({}, "//p:t")
    assert isinstance(absent, str)
    assert "not reported" in absent
    ambiguous = artifact_output({"//p:t": [artifact("a"), artifact("b")]}, "//p:t")
    assert isinstance(ambiguous, str)
    assert "2 default outputs" in ambiguous


def test_merge_deduplicates_shared_outputs_and_combines_test_status() -> None:
    output = artifact("bazel-out/bin/p/t.whl")
    one = Invocation(outputs=[output], test_status={"//p:a": "PASSED"})
    two = Invocation(outputs=[output], test_status={"//p:b": "FAILED"})
    merged = merge([one, two])
    assert merged is not None
    assert merged.outputs == [output]
    assert merged.test_status == {"//p:a": "PASSED", "//p:b": "FAILED"}
    assert merge([]) is None


if __name__ == "__main__":
    pytest_bazel.main()
