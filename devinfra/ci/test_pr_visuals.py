import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.pr_visuals import (
    DownloadedVisualTest,
    build_bundle,
    download_visual_tests,
    error_comment_body,
    find_test_invocations,
    success_comment_body,
    target_slug,
    upload_bundle,
)
from util.visual_review import VisualReviewAsset, VisualReviewManifest


def test_find_test_invocations_prefers_test_role_then_keeps_fallbacks(tmp_path: Path) -> None:
    (tmp_path / "linkage.json").write_text(
        json.dumps(
            {
                "buildbuddy": {
                    "bazel_invocations": [
                        {"role": "query", "invocation_id": "inv-query"},
                        {"role": "test", "invocation_id": "inv-test"},
                        {"role": "command-2", "invocation_id": "inv-real-test"},
                    ]
                }
            }
        )
    )
    assert find_test_invocations(tmp_path) == ["inv-test", "inv-query", "inv-real-test"]


def test_download_visual_tests_groups_manifests_by_executed_target(tmp_path: Path) -> None:
    artifacts = [
        {
            "label": "//haku/console/frontend:screenshots",
            "name": "test.outputs/visual-review.json",
            "uri": "bytestream://manifest-haku",
        },
        {
            "label": "//haku/console/frontend:screenshots",
            "name": "test.outputs/preview.png",
            "uri": "bytestream://preview-haku",
        },
        {
            "label": "//aiquota/gnome:test_render",
            "name": "test.outputs/visual-review.json",
            "uri": "bytestream://manifest-aiquota",
        },
        {"label": "//aiquota/gnome:test_render", "name": "test.outputs/hot.png", "uri": "bytestream://hot-aiquota"},
    ]
    commands: list[list[str | Path]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[2:4] == ["list", "missing"]:
            return subprocess.CompletedProcess(command, 1, "", "invocation not found")
        if command[2] == "list":
            return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

        match = str(command[4])
        output = Path(command[-1])
        if match.endswith("visual-review.json"):
            if match.startswith("//aiquota"):
                manifest = {
                    "schema": "ducktape.visual-review.v1",
                    "title": "AI quota",
                    "assets": [{"path": "hot.png", "label": "hot"}],
                }
            else:
                manifest = {
                    "schema": "ducktape.visual-review.v1",
                    "title": "Haku Console",
                    "assets": [{"path": "preview.png", "label": "preview"}],
                }
            output.write_text(json.dumps(manifest))
        else:
            output.write_bytes(b"png")
        return subprocess.CompletedProcess(command, 0, "", "")

    tests = download_visual_tests(["missing", "real"], tmp_path / "tests", run=fake_run)

    assert [test.target_label for test in tests] == [
        "//aiquota/gnome:test_render",
        "//haku/console/frontend:screenshots",
    ]
    assert (tests[0].directory / "hot.png").read_bytes() == b"png"
    assert commands[1] == [Path("bbapi"), "artifact", "list", "real", "--json"]


def test_download_visual_tests_rejects_missing_declared_asset(tmp_path: Path) -> None:
    artifacts = [
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "bytestream://manifest"}
    ]

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[2] == "list":
            return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")
        Path(command[-1]).write_text(
            json.dumps(
                {
                    "schema": "ducktape.visual-review.v1",
                    "title": "UI",
                    "assets": [{"path": "missing.png", "label": "missing"}],
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match=r"references missing artifact missing\.png"):
        download_visual_tests(["invocation"], tmp_path / "tests", run=fake_run)


def test_download_visual_tests_treats_null_artifact_list_as_empty(tmp_path: Path) -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "null", "")

    assert download_visual_tests(["invocation"], tmp_path / "tests", run=fake_run) == []


def test_build_bundle_groups_tests_and_writes_target_pages(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "screen.png").write_bytes(b"png")
    manifest = VisualReviewManifest.model_validate(
        {
            "schema": "ducktape.visual-review.v1",
            "title": "Example UI",
            "assets": [{"path": "screen.png", "label": "Screen"}],
        }
    )
    test = DownloadedVisualTest("//example:visuals", target_slug("//example:visuals"), manifest, source)
    sha = "0123456789abcdef0123456789abcdef01234567"

    bundle = build_bundle([test], tmp_path / "site", commit_sha=sha, repository="agentydragon/ducktape")

    assert bundle == tmp_path / "site" / "commits" / sha
    assert "//example:visuals" in (bundle / "index.html").read_text()
    target_page = bundle / "tests" / target_slug("//example:visuals") / "index.html"
    assert 'id="screen.png"' in target_page.read_text()
    assert (target_page.parent / "screen.png").read_bytes() == b"png"


def test_build_bundle_rejects_abbreviated_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        build_bundle([], tmp_path, commit_sha="0123456", repository="repo")


def test_manifest_rejects_paths_and_duplicates() -> None:
    with pytest.raises(ValueError, match="safe PNG basenames"):
        VisualReviewAsset(path="../secret.png", label="secret")
    with pytest.raises(ValueError, match="must be unique"):
        VisualReviewManifest.model_validate(
            {
                "schema": "ducktape.visual-review.v1",
                "title": "UI",
                "assets": [{"path": "same.png", "label": "one"}, {"path": "same.png", "label": "two"}],
            }
        )


def test_comment_bodies_link_commit_targets_and_report_errors() -> None:
    manifest = VisualReviewManifest.model_validate(
        {
            "schema": "ducktape.visual-review.v1",
            "title": "Example UI",
            "assets": [{"path": "screen.png", "label": "Screen"}],
        }
    )
    test = DownloadedVisualTest("//example:visuals", target_slug("//example:visuals"), manifest, Path("source"))
    sha = "0123456789abcdef0123456789abcdef01234567"

    success = success_comment_body(
        repository="agentydragon/ducktape", commit_sha=sha, url="https://visuals/commits/sha/", tests=[test]
    )
    failure = error_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        error=ValueError("//example:visuals references missing artifact screen.png"),
    )

    assert "commit/0123456789abcdef0123456789abcdef01234567" in success
    assert "tests/example-visuals-" in success
    assert "Visual review failed" in failure
    assert "missing artifact screen.png" in failure


def test_upload_publishes_all_indexes_last(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    target = bundle / "tests" / "example"
    target.mkdir(parents=True)
    (target / "screen.png").write_bytes(b"png")
    (target / "index.html").write_text("target")
    (bundle / "metadata.json").write_text("{}")
    (bundle / "index.html").write_text("root")

    class FakeS3:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def upload_file(self, _path: Path, _bucket: str, key: str, **kwargs: dict[str, str]) -> None:
            assert kwargs["ExtraArgs"]["CacheControl"].endswith("immutable")
            self.keys.append(key)

    client = FakeS3()
    upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="commits/sha", client=client)
    first_index = next(index for index, key in enumerate(client.keys) if key.endswith("index.html"))
    assert all(not key.endswith("index.html") for key in client.keys[:first_index])
    assert client.keys[-1] == "commits/sha/index.html"


if __name__ == "__main__":
    pytest_bazel.main()
