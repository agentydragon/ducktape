import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.pr_visuals import VisualManifest, build_bundle, find_test_invocation, upload_bundle


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


def test_manifest_rejects_paths() -> None:
    with pytest.raises(ValueError, match="safe PNG basenames"):
        VisualManifest.model_validate({"screenshots": ["../secret.png"]})


def test_upload_publishes_index_last(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "screen.png").write_bytes(b"png")
    (bundle / "metadata.json").write_text("{}")
    (bundle / "index.html").write_text("html")

    class FakeS3:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def upload_file(self, _path: str, _bucket: str, key: str, **kwargs: dict[str, str]) -> None:
            assert kwargs["ExtraArgs"]["CacheControl"].endswith("immutable")
            self.keys.append(key)

    client = FakeS3()
    upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="commits/sha/component", client=client)
    assert client.keys[-1] == "commits/sha/component/index.html"


if __name__ == "__main__":
    pytest_bazel.main()
