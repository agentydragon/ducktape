"""Tests for the framework-neutral test output writer."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from typing import IO

import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.testing.output_records import FILES_DIR, RECORDS_DIR, OutputRecord, output_key
from devinfra.testing.output_writer import BoundTestOutputs


def _publish_from_process(output_dir: str, suffix: str) -> str:
    try:
        BoundTestOutputs(Path(output_dir), "concurrent/example").publish(
            checkpoint="ready", role="screenshot", data=b"same payload", suffix=suffix
        )
    except FileExistsError:
        return "duplicate"
    return "published"


def test_output_key_uses_the_versioned_identity_tuple() -> None:
    expected = hashlib.sha256('["dialog/猫",null,"screenshot"]'.encode()).hexdigest()
    assert output_key("dialog/猫", None, "screenshot") == expected
    assert output_key("dialog/猫", "default", "screenshot") != expected
    assert output_key("dialog/猫", None, "diagnostics") != expected


def test_record_accepts_protocol_paths_without_writer_naming_conventions() -> None:
    record = OutputRecord.model_validate(
        {
            "schema": "ducktape.test-output.v1",
            "case_id": "dialog/mobile",
            "checkpoint": None,
            "role": "diagnostics",
            "path": "files/non-python-producer/trace.tar.gz",
        }
    )
    assert record.path == "files/non-python-producer/trace.tar.gz"


@pytest.mark.parametrize(
    ("field", "value"), [("case_id", ""), ("role", ""), ("checkpoint", ""), ("label", " \t"), ("title", " \t")]
)
def test_record_rejects_empty_identity_and_blank_display_text(field: str, value: str) -> None:
    record = {
        "schema": "ducktape.test-output.v1",
        "case_id": "example",
        "checkpoint": None,
        "role": "diagnostics",
        "path": "files/trace.json",
    }
    record[field] = value
    with pytest.raises(ValidationError) as exc:
        OutputRecord.model_validate(record)
    assert exc.value.errors()[0]["loc"] == (field,)


def test_record_requires_the_protocol_schema_version() -> None:
    with pytest.raises(ValidationError):
        OutputRecord.model_validate(
            {
                "schema": "ducktape.test-output.v2",
                "case_id": "example",
                "checkpoint": None,
                "role": "diagnostics",
                "path": "files/trace.json",
            }
        )


@pytest.mark.parametrize(
    "path", ["../secret", "/tmp/secret", "files/../secret", "files//secret", "files\\secret", "other/secret", "files/"]
)
def test_record_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        OutputRecord.model_validate(
            {
                "schema": "ducktape.test-output.v1",
                "case_id": "example",
                "checkpoint": None,
                "role": "diagnostics",
                "path": path,
            }
        )


def test_publish_output_writes_payload_before_record_with_stable_paths(tmp_path: Path) -> None:
    payload_path = BoundTestOutputs(tmp_path, "dialog/mobile").publish(
        checkpoint="expanded",
        role="screenshot",
        data=b"complete image bytes",
        suffix="png",
        label="Expanded dialog on mobile",
        title="Dialog review",
    )
    key = output_key("dialog/mobile", "expanded", "screenshot")
    record_path = tmp_path / "test-outputs" / RECORDS_DIR / f"{key}.json"
    assert payload_path == tmp_path / "test-outputs" / FILES_DIR / f"{key}.png"
    assert payload_path.read_bytes() == b"complete image bytes"
    assert json.loads(record_path.read_text()) == {
        "schema": "ducktape.test-output.v1",
        "case_id": "dialog/mobile",
        "checkpoint": "expanded",
        "role": "screenshot",
        "path": f"files/{key}.png",
        "label": "Expanded dialog on mobile",
        "title": "Dialog review",
    }


def test_publish_file_copies_source_and_preserves_compound_suffixes(tmp_path: Path) -> None:
    source = tmp_path / "trace.tar.gz"
    source.write_bytes(b"compressed trace")

    payload_path = BoundTestOutputs(tmp_path / "outputs", "dialog/mobile").publish_file(
        checkpoint=None, role="diagnostics", source=source, label="Browser trace", title="Diagnostic capture"
    )

    key = output_key("dialog/mobile", None, "diagnostics")
    record_path = tmp_path / "outputs" / "test-outputs" / RECORDS_DIR / f"{key}.json"
    assert payload_path.name == f"{key}.tar.gz"
    assert payload_path.read_bytes() == b"compressed trace"
    assert json.loads(record_path.read_text()) == {
        "schema": "ducktape.test-output.v1",
        "case_id": "dialog/mobile",
        "checkpoint": None,
        "role": "diagnostics",
        "path": f"files/{key}.tar.gz",
        "label": "Browser trace",
        "title": "Diagnostic capture",
    }


def test_publish_output_accepts_utf8_text(tmp_path: Path) -> None:
    payload = BoundTestOutputs(tmp_path, "example").publish(role="diagnostics", data='{"message":"猫"}', suffix="json")
    assert payload.read_bytes() == b'{"message":"\xe7\x8c\xab"}'


def test_publish_file_discards_interrupted_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "trace.zip"
    source.write_bytes(b"complete trace")
    outputs = BoundTestOutputs(tmp_path / "out", "example")
    root = outputs.output_dir / "test-outputs"

    def interrupted_copy(input_file: IO[bytes], output_file: IO[bytes]) -> None:
        output_file.write(input_file.read(1))
        assert not list((root / RECORDS_DIR).iterdir())
        raise OSError("copy interrupted")

    with monkeypatch.context() as patch:
        patch.setattr("devinfra.testing.output_writer.shutil.copyfileobj", interrupted_copy)
        with pytest.raises(OSError, match="copy interrupted"):
            outputs.publish_file(source=source, role="trace")

    assert not list((root / RECORDS_DIR).iterdir())
    assert not list((root / FILES_DIR).iterdir())
    assert outputs.publish_file(source=source, role="trace").read_bytes() == b"complete trace"


def test_publish_file_duplicate_claim_fails(tmp_path: Path) -> None:
    outputs = BoundTestOutputs(tmp_path / "outputs", "example")
    source = tmp_path / "capture.png"
    source.write_bytes(b"image bytes")

    first = outputs.publish_file(checkpoint="default", role="screenshot", source=source)
    with pytest.raises(FileExistsError):
        outputs.publish_file(checkpoint="default", role="screenshot", source=source)

    assert first.read_bytes() == b"image bytes"
    assert len(list((tmp_path / "outputs" / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1


def test_publish_file_rejects_identity_claimed_by_publish_with_another_suffix(tmp_path: Path) -> None:
    outputs = BoundTestOutputs(tmp_path, "example")
    source = tmp_path / "diagnostics.txt"
    source.write_bytes(b"same")
    first = outputs.publish(role="diagnostics", data=b"same", suffix="json")
    with pytest.raises(FileExistsError):
        outputs.publish_file(role="diagnostics", source=source)
    assert first.read_bytes() == b"same"
    assert list(first.parent.iterdir()) == [first]
    assert len(list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1


def test_writer_serializes_null_checkpoint_and_omits_optional_gallery_fields(tmp_path: Path) -> None:
    payload = BoundTestOutputs(tmp_path, "example").publish(
        checkpoint=None, role="diagnostics", data=b"diagnostics", suffix="json"
    )
    key = output_key("example", None, "diagnostics")
    record_path = tmp_path / "test-outputs" / RECORDS_DIR / f"{key}.json"
    record = json.loads(record_path.read_text())

    assert payload.read_bytes() == b"diagnostics"
    assert "checkpoint" in record
    assert record["checkpoint"] is None
    assert "label" not in record
    assert "title" not in record


def test_writer_rejects_orphan_payload_without_overwriting_it(tmp_path: Path) -> None:
    key = output_key("orphaned", None, "diagnostics")
    files_dir = tmp_path / "test-outputs" / FILES_DIR
    files_dir.mkdir(parents=True)
    payload_path = files_dir / f"{key}.json"
    payload_path.write_bytes(b"existing orphan")

    with pytest.raises(FileExistsError):
        BoundTestOutputs(tmp_path, "orphaned").publish(
            checkpoint=None, role="diagnostics", data=b"replacement must fail", suffix="json"
        )

    assert payload_path.read_bytes() == b"existing orphan"
    assert list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json")) == []


@pytest.mark.parametrize("suffixes", [("png",) * 6, ("png", "jpg") * 3], ids=["same-suffix", "mixed-suffixes"])
def test_parallel_processes_claim_one_identity_once(tmp_path: Path, suffixes: tuple[str, ...]) -> None:
    context = multiprocessing.get_context("fork")
    with context.Pool(processes=len(suffixes)) as pool:
        results = pool.starmap(_publish_from_process, [(os.fspath(tmp_path), suffix) for suffix in suffixes])

    assert results.count("published") == 1
    assert results.count("duplicate") == 5
    assert len(list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1
    assert len(list((tmp_path / "test-outputs" / FILES_DIR).iterdir())) == 1


def test_writer_hashes_unsafe_case_ids_and_rejects_unsafe_suffixes(tmp_path: Path) -> None:
    path = BoundTestOutputs(tmp_path, "../../outside").publish(
        checkpoint=None, role="screenshot", data=b"safe", suffix="png"
    )
    assert path.parent == tmp_path / "test-outputs" / FILES_DIR
    with pytest.raises(ValueError, match="suffix"):
        BoundTestOutputs(tmp_path, "other").publish(
            checkpoint=None, role="screenshot", data=b"unsafe", suffix="../outside"
        )
    assert not (tmp_path / "outside").exists()


def test_writer_rejects_symlinked_payload_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    root = output_dir / "test-outputs"
    root.mkdir(parents=True)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (root / FILES_DIR).symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink or file"):
        BoundTestOutputs(output_dir, "example").publish(
            checkpoint=None, role="diagnostics", data=b"must remain here", suffix="json"
        )
    assert list(external_dir.iterdir()) == []


if __name__ == "__main__":
    pytest_bazel.main()
