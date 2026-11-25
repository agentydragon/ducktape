"""Verify that frozen input provenance follows the bytes actually consumed."""

import hashlib
from pathlib import Path

import pytest
import pytest_bazel

from x.local_llm.eval import frozen_inputs


def test_frozen_prompts_match_the_pinned_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "prompts.jsonl"
    original = b'{"task_id": "test/1"}\n'
    path.write_bytes(original)
    monkeypatch.setattr(frozen_inputs, "PROMPTS_SHA256", hashlib.sha256(original).hexdigest())

    assert frozen_inputs.read_frozen_prompts(path) == original
    path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="expected frozen prompts with SHA-256"):
        frozen_inputs.read_frozen_prompts(path)


def test_cohort_digest_uses_the_parsed_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cohort.json"
    original = b'{ "task_ids": ["test/1"] }\n'
    path.write_bytes(original)

    read_bytes = Path.read_bytes

    def replace_after_read(source: Path) -> bytes:
        raw = read_bytes(source)
        if source == path:
            path.write_bytes(b'{"task_ids": ["test/2"]}')
        return raw

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)

    assert frozen_inputs.read_frozen_cohort(path) == ({"task_ids": ["test/1"]}, hashlib.sha256(original).hexdigest())


if __name__ == "__main__":
    pytest_bazel.main()
