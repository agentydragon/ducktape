"""Tests for the framework-neutral test output writer."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.testing.output_records import FILES_DIR, RECORDS_DIR, OutputRecord, output_key
from devinfra.testing.output_writer import open_output, publish_file, publish_output


def _publish_from_process(output_dir: str) -> str:
    try:
        publish_output(
            Path(output_dir),
            case_id="concurrent/example",
            checkpoint="ready",
            role="screenshot",
            data=b"same payload",
            suffix="png",
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
    payload_path = publish_output(
        tmp_path,
        case_id="dialog/mobile",
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

    payload_path = publish_file(
        tmp_path / "outputs",
        case_id="dialog/mobile",
        checkpoint=None,
        role="diagnostics",
        source=source,
        label="Browser trace",
        title="Diagnostic capture",
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
    payload = publish_output(tmp_path, case_id="example", role="diagnostics", data='{"message":"猫"}', suffix="json")
    assert payload.read_bytes() == b'{"message":"\xe7\x8c\xab"}'


def test_open_output_publishes_text_and_binary_after_successful_close(tmp_path: Path) -> None:
    with open_output(tmp_path, case_id="example", role="diagnostics", suffix="json") as output:
        output.write('{"message":"猫"}')
        assert output.seekable()
        assert list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json")) == []
    text_path = tmp_path / "test-outputs" / FILES_DIR / f"{output_key('example', None, 'diagnostics')}.json"
    assert text_path.read_text() == '{"message":"猫"}'

    with open_output(tmp_path, case_id="example", role="screenshot", suffix="png", mode="wb") as output:
        output.write(b"image bytes")
    image_path = tmp_path / "test-outputs" / FILES_DIR / f"{output_key('example', None, 'screenshot')}.png"
    assert image_path.read_bytes() == b"image bytes"


def test_open_output_exception_discards_data_and_duplicate_claim_preserves_original(tmp_path: Path) -> None:
    def fail_during_write() -> None:
        with open_output(tmp_path, case_id="example", role="diagnostics", suffix="json") as output:
            output.write("incomplete")
            raise RuntimeError("capture failed")

    with pytest.raises(RuntimeError, match="capture failed"):
        fail_during_write()
    assert list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json")) == []
    assert list((tmp_path / "test-outputs" / FILES_DIR).iterdir()) == []

    with open_output(tmp_path, case_id="example", role="diagnostics", suffix="json") as output:
        output.write("complete")
    with (
        pytest.raises(FileExistsError, match="already claimed"),
        open_output(tmp_path, case_id="example", role="diagnostics", suffix="json") as output,
    ):
        output.write("replacement")
    key = output_key("example", None, "diagnostics")
    assert (tmp_path / "test-outputs" / FILES_DIR / f"{key}.json").read_text() == "complete"
    assert len(list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1
    assert len(list((tmp_path / "test-outputs" / FILES_DIR).iterdir())) == 1


def test_publish_file_duplicate_claim_fails(tmp_path: Path) -> None:
    source = tmp_path / "capture.png"
    source.write_bytes(b"image bytes")

    first = publish_file(
        tmp_path / "outputs", case_id="example", checkpoint="default", role="screenshot", source=source
    )
    with pytest.raises(FileExistsError):
        publish_file(tmp_path / "outputs", case_id="example", checkpoint="default", role="screenshot", source=source)

    assert first.read_bytes() == b"image bytes"
    assert len(list((tmp_path / "outputs" / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1


def test_duplicate_claim_fails_even_when_payload_is_equal(tmp_path: Path) -> None:
    first = publish_output(
        tmp_path, case_id="example", checkpoint=None, role="diagnostics", data=b"same", suffix="json"
    )
    with pytest.raises(FileExistsError):
        publish_output(tmp_path, case_id="example", checkpoint=None, role="diagnostics", data=b"same", suffix="txt")
    assert first.read_bytes() == b"same"
    assert len(list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1


def test_writer_serializes_null_checkpoint_and_omits_optional_gallery_fields(tmp_path: Path) -> None:
    payload = publish_output(
        tmp_path, case_id="example", checkpoint=None, role="diagnostics", data=b"diagnostics", suffix="json"
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
        publish_output(
            tmp_path,
            case_id="orphaned",
            checkpoint=None,
            role="diagnostics",
            data=b"replacement must fail",
            suffix="json",
        )

    assert payload_path.read_bytes() == b"existing orphan"
    assert list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json")) == []


def test_parallel_processes_claim_one_identity_once(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    with context.Pool(processes=6) as pool:
        results = pool.map(_publish_from_process, [os.fspath(tmp_path)] * 6)

    assert results.count("published") == 1
    assert results.count("duplicate") == 5
    assert len(list((tmp_path / "test-outputs" / RECORDS_DIR).glob("*.json"))) == 1
    assert len(list((tmp_path / "test-outputs" / FILES_DIR).iterdir())) == 1


def test_writer_hashes_unsafe_case_ids_and_rejects_unsafe_suffixes(tmp_path: Path) -> None:
    path = publish_output(
        tmp_path, case_id="../../outside", checkpoint=None, role="screenshot", data=b"safe", suffix="png"
    )
    assert path.parent == tmp_path / "test-outputs" / FILES_DIR
    with pytest.raises(ValueError, match="suffix"):
        publish_output(
            tmp_path, case_id="other", checkpoint=None, role="screenshot", data=b"unsafe", suffix="../outside"
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
        publish_output(
            output_dir, case_id="example", checkpoint=None, role="diagnostics", data=b"must remain here", suffix="json"
        )
    assert list(external_dir.iterdir()) == []


if __name__ == "__main__":
    pytest_bazel.main()
