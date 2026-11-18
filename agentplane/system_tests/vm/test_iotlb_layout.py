"""Exercise the RPM/OCI boundary without network access or a running guest."""

import io
import json
import struct
import tarfile
from pathlib import Path

import pytest
import zstandard

from agentplane.system_tests.vm.iotlb_layout import extract_debug, image_binary


def rpm(payload: bytes) -> bytes:
    header = struct.pack(">8sII", b"\x8e\xad\xe8\x01\0\0\0\0", 0, 0)
    return b"\xed\xab\xee\xdb" + bytes(92) + header + header + zstandard.ZstdCompressor().compress(payload)


def member(name: str, data: bytes) -> bytes:
    fields = [1, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0]
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in fields) + name.encode() + b"\0"
    header += bytes(-len(header) % 4)
    result = header + data
    return result + bytes(-len(result) % 4)


def test_extracts_only_named_rpm_member_and_rejects_truncation() -> None:
    archive = member("./unrelated", b"other") + member("./test.debug", b"DWARF")
    assert extract_debug(rpm(archive), "test.debug") == b"DWARF"
    with pytest.raises(ValueError, match="absent"):
        extract_debug(rpm(archive), "missing")
    with pytest.raises(ValueError, match="Truncated"):
        extract_debug(rpm(member("test.debug", b"DWARF")[:-4]), "test.debug")


def test_oci_layers_respect_replacement_and_whiteout(tmp_path: Path) -> None:
    blobs = tmp_path / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (tmp_path / "index.json").write_text(json.dumps({"manifests": [{"digest": "sha256:manifest"}]}))
    layers = []
    for index, (name, content) in enumerate(
        [("usr/libexec/qemu-kvm", b"old"), ("usr/libexec/qemu-kvm", b"new"), ("usr/libexec/.wh.qemu-kvm", b"")]
    ):
        path = blobs / str(index)
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        layers.append({"digest": f"sha256:{index}"})
    (blobs / "manifest").write_text(json.dumps({"layers": layers[:2]}))
    assert image_binary(tmp_path) == b"new"
    (blobs / "manifest").write_text(json.dumps({"layers": layers}))
    with pytest.raises(ValueError, match="absent"):
        image_binary(tmp_path)
