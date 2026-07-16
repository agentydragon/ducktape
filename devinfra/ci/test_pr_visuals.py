import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image

from devinfra.ci.pr_visuals import (
    COMMENT_BUDGET,
    ClassificationCounts,
    DownloadedVisualTest,
    ReviewAsset,
    ReviewTest,
    build_bundle,
    download_visual_tests,
    error_comment_body,
    find_test_invocations,
    success_comment_body,
    target_slug,
    upload_bundle,
)
from util.visual_diff import compare_pngs
from util.visual_review import VisualReviewAsset, VisualReviewManifest


def _png(path: Path, color: tuple[int, int, int, int] = (10, 20, 30, 255), size: tuple[int, int] = (8, 8)) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


class FakeBaselineSource:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str) -> bytes | None:
        return self.objects.get(key)


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
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(
            target_label="//example:visuals",
            slug=target_slug("//example:visuals"),
            title="Example UI",
            assets=[ReviewAsset(path="screen.png", label="Screen")],
        )
    ]

    success = success_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        url="https://visuals/commits/sha/",
        review_tests=review_tests,
    )
    failure = error_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        error=ValueError("//example:visuals references missing artifact screen.png"),
    )

    assert "commit/0123456789abcdef0123456789abcdef01234567" in success
    assert "[Open visual review](https://visuals/commits/sha/index.html)" in success
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


def test_compare_pngs_classifies_exact_diffs(tmp_path: Path) -> None:
    identical = _png(tmp_path / "identical.png")
    assert compare_pngs(_png(tmp_path / "a.png"), identical).classification == "unchanged"

    modified = compare_pngs(_png(tmp_path / "a.png"), _png(tmp_path / "b.png", (10, 20, 31, 255)))
    assert modified.classification == "modified"
    assert modified.changed_pixels == 64
    assert modified.dimension_changed is False
    assert modified.diff_overlay is not None

    resized = compare_pngs(_png(tmp_path / "a.png"), _png(tmp_path / "c.png", size=(8, 10)))
    assert resized.classification == "modified"
    assert resized.dimension_changed is True


def test_build_bundle_classifies_against_baseline(tmp_path: Path) -> None:
    slug = target_slug("//ex:visuals")
    base_sha = "fedcba9876543210fedcba9876543210fedcba98"
    head_sha = "0123456789abcdef0123456789abcdef01234567"

    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    _png(candidate_dir / "same.png", (10, 20, 30, 255))
    _png(candidate_dir / "changed.png", (40, 50, 60, 255))
    _png(candidate_dir / "added.png", (1, 2, 3, 255))
    manifest = VisualReviewManifest.model_validate(
        {
            "schema": "ducktape.visual-review.v1",
            "title": "Ex",
            "assets": [
                {"path": "same.png", "label": "same"},
                {"path": "changed.png", "label": "changed"},
                {"path": "added.png", "label": "added"},
            ],
        }
    )
    test = DownloadedVisualTest("//ex:visuals", slug, manifest, candidate_dir)

    objects: dict[str, bytes] = {
        f"commits/{base_sha}/tests/{slug}/metadata.json": json.dumps(
            {
                "target_label": "//ex:visuals",
                "slug": slug,
                "title": "Ex",
                "assets": [
                    {"path": "same.png", "label": "same"},
                    {"path": "changed.png", "label": "changed"},
                    {"path": "gone.png", "label": "gone"},
                ],
            }
        ).encode(),
        f"commits/{base_sha}/tests/{slug}/same.png": (candidate_dir / "same.png").read_bytes(),
        f"commits/{base_sha}/tests/{slug}/changed.png": _png(
            tmp_path / "changed_base.png", (70, 80, 90, 255)
        ).read_bytes(),
        f"commits/{base_sha}/tests/{slug}/gone.png": _png(tmp_path / "gone_base.png", (5, 6, 7, 255)).read_bytes(),
    }

    bundle = build_bundle(
        [test],
        tmp_path / "site",
        commit_sha=head_sha,
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource(objects),
    )

    test_dir = bundle / "tests" / slug
    metadata = json.loads((test_dir / "metadata.json").read_text())
    by_path = {asset["path"]: asset for asset in metadata["assets"]}
    assert by_path["same.png"]["classification"] == "unchanged"
    assert by_path["changed.png"]["classification"] == "modified"
    assert by_path["added.png"]["classification"] == "new"
    assert by_path["gone.png"]["classification"] == "removed"
    assert metadata["summary"] == {"modified": 1, "new": 1, "removed": 1, "unchanged": 1}

    assert (test_dir / "baseline" / "same.png").exists()
    assert (test_dir / "baseline" / "changed.png").exists()
    assert (test_dir / "baseline" / "gone.png").exists()
    assert (test_dir / "diff" / "changed.png").exists()
    assert not (test_dir / "diff" / "same.png").exists()
    for candidate in ("same.png", "changed.png", "added.png"):
        assert (test_dir / candidate).exists()
    assert not (test_dir / "gone.png").exists()


def test_success_comment_body_reports_counts_and_previews() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(
            target_label="//ex:visuals",
            slug="ex-visuals-abcdef",
            title="Ex",
            summary=ClassificationCounts(modified=2, new=1, removed=0, unchanged=1),
            assets=[
                ReviewAsset(
                    path="a.png", label="a", classification="modified", changed_fraction=0.5, changed_pixels=10
                ),
                ReviewAsset(path="b.png", label="b", classification="modified", changed_fraction=0.2, changed_pixels=4),
                ReviewAsset(path="c.png", label="c", classification="new"),
                ReviewAsset(path="d.png", label="d", classification="unchanged"),
            ],
        )
    ]
    body = success_comment_body(
        repository="r", commit_sha=sha, url="https://v/commits/sha/", review_tests=review_tests, base_sha="f" * 40
    )
    assert "**2 modified**, 1 new, 0 removed, 1 unchanged" in body
    assert "tests/ex-visuals-abcdef/diff/a.png" in body
    assert "tests/ex-visuals-abcdef/diff/b.png" in body
    assert "50.0% changed" in body


def test_success_comment_body_drops_previews_over_budget() -> None:
    big = "x" * 3500
    review_tests = [
        ReviewTest(
            target_label="//t:a",
            slug="s",
            title="T",
            summary=ClassificationCounts(modified=2),
            assets=[
                ReviewAsset(path="a.png", label=big, classification="modified", changed_fraction=0.9, changed_pixels=1),
                ReviewAsset(path="b.png", label=big, classification="modified", changed_fraction=0.1, changed_pixels=1),
            ],
        )
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
    )
    assert body.count("<img ") == 1
    assert "diff/a.png" in body
    assert len(body) <= COMMENT_BUDGET


if __name__ == "__main__":
    pytest_bazel.main()
