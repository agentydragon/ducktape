import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.pr_visuals import build_bundle, find_test_invocation


def test_build_bundle_uses_full_sha_and_component(tmp_path: Path) -> None:
    source = tmp_path / "screenshots"
    source.mkdir()
    (source / "desktop-light.png").write_bytes(b"png")
    sha = "0123456789abcdef0123456789abcdef01234567"
    bundle = build_bundle(
        source,
        tmp_path / "site",
        commit_sha=sha,
        component="example-ui",
        title="Example UI",
        repository="agentydragon/ducktape",
    )
    assert bundle == tmp_path / "site" / "commits" / sha / "example-ui"
    assert 'src="desktop-light.png"' in (bundle / "index.html").read_text()
    assert json.loads((bundle / "metadata.json").read_text())["component"] == "example-ui"


def test_build_bundle_rejects_abbreviated_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        build_bundle(tmp_path, tmp_path, commit_sha="0123456", component="ui", title="UI", repository="repo")


def test_find_test_invocation_reads_linkage(tmp_path: Path) -> None:
    (tmp_path / "linkage.json").write_text(
        json.dumps({"buildbuddy": {"bazel_invocations": [{"role": "test", "invocation_id": "inv-1"}]}})
    )
    assert find_test_invocation(tmp_path) == "inv-1"


if __name__ == "__main__":
    pytest_bazel.main()
