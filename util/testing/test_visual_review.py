import json
from pathlib import Path

import pydantic
import pytest
import pytest_bazel

from util.testing.visual_review import upsert_review_asset, write_visual_review_manifest
from util.visual_review import VisualReviewAsset


def test_write_visual_review_manifest(tmp_path: Path) -> None:
    destination = write_visual_review_manifest(
        tmp_path, title="Example UI", assets=[VisualReviewAsset(path="screen.png", label="Screen")]
    )

    assert json.loads(destination.read_text()) == {
        "schema": "ducktape.visual-review.v1",
        "title": "Example UI",
        "assets": [{"path": "screen.png", "label": "Screen"}],
    }


def test_labels_keep_their_non_ascii_characters_across_upserts(tmp_path: Path) -> None:
    upsert_review_asset(tmp_path, title="Example UI", asset=VisualReviewAsset(path="a.png", label="hot · dark"))
    destination = upsert_review_asset(tmp_path, title="Example UI", asset=VisualReviewAsset(path="b.png", label="B"))

    assert '"label": "hot · dark"' in destination.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("title", "path"),
    [
        ("Example UI", "../screen.png"),
        ("Example UI", "dir/screen.png"),
        ("Example UI", "screen.jpg"),
        ("  ", "screen.png"),
    ],
)
def test_manifest_publishes_only_safe_png_basenames_under_a_title(tmp_path: Path, title: str, path: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        write_visual_review_manifest(tmp_path, title=title, assets=[VisualReviewAsset(path=path, label="Screen")])

    assert not (tmp_path / "visual-review.json").exists()


def test_upsert_review_asset_accumulates_and_keeps_the_first_label_of_a_path(tmp_path: Path) -> None:
    upsert_review_asset(tmp_path, title="Example UI", asset=VisualReviewAsset(path="a.png", label="A"))
    upsert_review_asset(tmp_path, title="Example UI", asset=VisualReviewAsset(path="b.png", label="B"))
    upserted = upsert_review_asset(tmp_path, title="Example UI", asset=VisualReviewAsset(path="a.png", label="A2"))

    assert json.loads(upserted.read_text()) == {
        "schema": "ducktape.visual-review.v1",
        "title": "Example UI",
        "assets": [{"path": "a.png", "label": "A"}, {"path": "b.png", "label": "B"}],
    }


if __name__ == "__main__":
    pytest_bazel.main()
