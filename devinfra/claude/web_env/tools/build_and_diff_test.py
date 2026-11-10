import io
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Never

import pytest
import pytest_bazel

from devinfra.claude.web_env.tools import build_and_diff


@pytest.fixture
def image() -> Iterator[str]:
    tag = f"build-and-diff-test-{uuid.uuid4().hex}:latest"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        contents = b"manifest evidence"
        entry = tarfile.TarInfo("evidence")
        entry.size = len(contents)
        tar.addfile(entry, io.BytesIO(contents))
    with ExitStack() as stack:
        subprocess.run(["docker", "import", "-", tag], input=archive.getvalue(), check=True, capture_output=True)
        stack.callback(subprocess.run, ["docker", "image", "rm", "-f", tag], check=False, capture_output=True)
        yield tag


@pytest.mark.parametrize("capture_fails", [False, True])
def test_capture_cleans_container_and_rootfs(
    image: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_fails: bool
) -> None:
    container = f"build-and-diff-test-{uuid.uuid4().hex}"
    monkeypatch.setattr(build_and_diff, "IMAGE_NAME", image)
    monkeypatch.setattr(build_and_diff, "CONTAINER_NAME", container)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    if capture_fails:

        def fail_capture(*, root: Path) -> Never:
            assert (root / "evidence").read_bytes() == b"manifest evidence"
            raise RuntimeError("capture failed")

        monkeypatch.setattr(build_and_diff, "capture", fail_capture)
        with pytest.raises(RuntimeError, match="capture failed"):
            build_and_diff.capture_built_manifest()
    else:
        assert "/evidence" in build_and_diff.capture_built_manifest()

    assert not list(tmp_path.glob("built_rootfs_*"))
    result = subprocess.run(["docker", "inspect", container], check=False, capture_output=True)
    assert result.returncode != 0


if __name__ == "__main__":
    pytest_bazel.main()
