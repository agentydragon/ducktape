"""The GGUF rewrite retains the model's tensor table and tensor payload."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest
import pytest_bazel
from jinja2 import Environment

from cluster.cdk8s.ollama.patch_qwen38_template import (
    NEW_BRANCH,
    OLD_BRANCH,
    ORIGINAL_TEMPLATE_SHA256,
    Reader,
    replace_template,
)
from util.bazel.runfiles import get_required_path


def _template_path() -> Path:
    return get_required_path("_main/cluster/k8s/ollama/qwen38-chat-template.jinja")


def _string(value: bytes) -> bytes:
    return struct.pack("<Q", len(value)) + value


def _kv(key: str, kind: int, value: bytes) -> bytes:
    return _string(key.encode()) + struct.pack("<I", kind) + value


def _source_template(replacement: bytes) -> bytes:
    replacement = replacement.removesuffix(b"\n")
    assert replacement.count(NEW_BRANCH) == 1
    original = replacement.replace(NEW_BRANCH, OLD_BRANCH)
    assert hashlib.sha256(original).hexdigest() == ORIGINAL_TEMPLATE_SHA256
    return original


def _gguf(original_template: bytes) -> tuple[bytes, bytes]:
    metadata = b"".join(
        (
            _kv("general.alignment", 4, struct.pack("<I", 64)),
            _kv("tokenizer.chat_template", 8, _string(original_template)),
            _kv("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 2) + _string(b"a") + _string(b"bb")),
        )
    )
    tensor_info = _string(b"blk.0.weight") + struct.pack("<IQQIQ", 2, 3, 5, 0, 0)
    header = b"GGUF" + struct.pack("<IQQ", 3, 1, 3) + metadata + tensor_info
    payload = b"immutable tensor payload" * 3
    return header + bytes((-len(header)) % 64) + payload, payload


def test_changes_only_chat_template_and_realigns_tensor_data() -> None:
    replacement = _template_path().read_bytes()
    source, payload = _gguf(_source_template(replacement))
    patched = replace_template(source, replacement)
    assert patched.endswith(payload)
    assert patched.count(payload) == 1
    assert patched[:24] == source[:24]  # magic, version, tensor and metadata counts
    assert _string(b"blk.0.weight") + struct.pack("<IQQIQ", 2, 3, 5, 0, 0) in patched
    assert patched.count(replacement.removesuffix(b"\n")) == 1
    assert len(patched) % 64 == len(payload) % 64


def test_refuses_any_other_template_change() -> None:
    replacement = _template_path().read_bytes()
    source, _ = _gguf(_source_template(replacement))
    with pytest.raises(ValueError, match="replacement changes more"):
        replace_template(source, replacement + b"extra")


def test_rejects_truncated_metadata() -> None:
    with pytest.raises(ValueError, match="truncated"):
        Reader(b"\x01").u64()


@pytest.mark.parametrize(
    "roles",
    [["system", "developer", "user"], ["system", "user", "system", "user"], ["system", "user", "developer", "user"]],
)
def test_template_preserves_instruction_position(roles: list[str]) -> None:
    template = _template_path().read_text()
    messages = [{"role": role, "content": f"part-{index}"} for index, role in enumerate(roles)]
    rendered = (
        Environment()
        .from_string(template)
        .render(messages=messages, tools=[], enable_thinking=False, add_generation_prompt=True)
    )
    expected_roles = ["system" if role == "developer" else role for role in roles]
    if len(roles) >= 2 and roles[:2] == ["system", "developer"]:
        expected_roles = ["system", *expected_roles[2:]]
    actual_roles = [part.split("\n", 1)[0] for part in rendered.split("<|im_start|>")[1:]]
    assert actual_roles == [*expected_roles, "assistant"]
    assert all(f"part-{index}" in rendered for index in range(len(roles)))
    assert rendered.index("part-0") < rendered.index("part-1")
    if len(roles) == 4:
        assert rendered.index("part-1") < rendered.index("part-2") < rendered.index("part-3")


if __name__ == "__main__":
    pytest_bazel.main()
