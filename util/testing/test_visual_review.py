import json
from pathlib import Path

import pytest
import pytest_bazel

from util.testing.visual_review import VisualReviewAsset, write_visual_review_manifest


def test_write_visual_review_manifest(tmp_path: Path) -> None:
    destination = write_visual_review_manifest(
        tmp_path, title="Example UI", assets=[VisualReviewAsset(path="screen.png", label="Screen")]
    )

    assert json.loads(destination.read_text()) == {
        "schema": "ducktape.visual-review.v1",
        "title": "Example UI",
        "assets": [{"path": "screen.png", "label": "Screen"}],
    }


def test_write_visual_review_manifest_rejects_unsafe_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="safe PNG basenames"):
        write_visual_review_manifest(
            tmp_path, title="Example UI", assets=[VisualReviewAsset(path="../screen.png", label="Screen")]
        )


if __name__ == "__main__":
    pytest_bazel.main()
