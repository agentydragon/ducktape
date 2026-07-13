from __future__ import annotations

import pytest_bazel

from haku.console.deployment import build_deployment_info


def test_build_deployment_info_parses_flux_image_tags() -> None:
    info = build_deployment_info(
        {
            "HAKU_CONSOLE_IMAGE_TAG": "devel-20260713014452-83da566",
            "HAKU_CONSOLE_STATIC_IMAGE_TAG": "devel-20260713015518-bfad4bf",
        }
    )

    assert info.server.source_commit == "83da566"
    assert info.server.source_commit_url == "https://github.com/agentydragon/ducktape/commit/83da566"
    assert info.frontend.source_commit == "bfad4bf"
    assert info.frontend.source_commit_url == "https://github.com/agentydragon/ducktape/commit/bfad4bf"


def test_build_deployment_info_rejects_non_automation_tags() -> None:
    info = build_deployment_info({"HAKU_CONSOLE_IMAGE_TAG": "latest", "HAKU_CONSOLE_STATIC_IMAGE_TAG": "  "})

    assert info.server.image_tag == "latest"
    assert info.server.source_commit is None
    assert info.server.source_commit_url is None
    assert info.frontend.image_tag is None
    assert info.frontend.source_commit is None


if __name__ == "__main__":
    pytest_bazel.main()
