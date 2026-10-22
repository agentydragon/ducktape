import pytest

from util.visual_review import VisualReviewAsset, VisualReviewManifest


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
