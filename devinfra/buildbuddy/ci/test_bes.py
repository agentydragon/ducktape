import json

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci import bes
from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, artifact_output, merge, read
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
  }]
}"""


def test_read_adapts_structured_output_export(monkeypatch) -> None:
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


def test_read_wraps_cli_errors(monkeypatch) -> None:
    def fail(*_args: str, **_kwargs) -> bytes:
        raise CLIError("bbapi invocation export failed")

    monkeypatch.setattr(bes, "run", fail)
    with pytest.raises(BuildBuddyError, match="bbapi invocation export failed"):
        read("missing")


def test_read_rejects_invalid_export(monkeypatch) -> None:
    monkeypatch.setattr(bes, "run", lambda *_args, **_kwargs: b'{"outputs": [{}]}')
    with pytest.raises(BuildBuddyError, match="invalid JSON"):
        read("inv-id")


def test_read_rejects_a_stream_from_another_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.loads(EXPORT)
    document["invocationId"] = "another-invocation"
    monkeypatch.setattr(bes, "run", lambda *_args, **_kwargs: json.dumps(document).encode())
    with pytest.raises(BuildBuddyError, match="requested invocation expected, received another-invocation"):
        read("expected")


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


def test_merge_deduplicates_shared_outputs() -> None:
    output = artifact("bazel-out/bin/p/t.whl")
    one = Invocation(outputs=[output])
    two = Invocation(outputs=[output])
    merged = merge([one, two])
    assert merged is not None
    assert merged.outputs == [output]
    assert merge([]) is None


if __name__ == "__main__":
    pytest_bazel.main()
