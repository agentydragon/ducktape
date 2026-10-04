from __future__ import annotations

import pytest_bazel

from haku.console.deployment import build_deployment_info


def test_build_deployment_info_prefers_projected_static_tag(tmp_path) -> None:
    tag_file = tmp_path / "image-tag"
    tag_file.write_text("devel-20260819010101-abcdef0\n", encoding="utf-8")

    info = build_deployment_info(
        image_tag="devel-20260713014452-83da566",
        static_image_tag="devel-20260713015518-stale00",
        static_image_tag_file=tag_file,
    )

    assert info.frontend.image_tag == "devel-20260819010101-abcdef0"
    assert info.frontend.source_commit == "abcdef0"


def test_build_deployment_info_tolerates_missing_projected_static_tag(tmp_path) -> None:
    info = build_deployment_info(
        image_tag=None, static_image_tag="devel-20260713015518-bfad4bf", static_image_tag_file=tmp_path / "missing"
    )

    assert info.frontend.source_commit == "bfad4bf"


if __name__ == "__main__":
    pytest_bazel.main()
