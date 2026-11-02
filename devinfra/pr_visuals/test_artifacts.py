import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.pr_visuals.artifacts import list_ci_artifacts


def test_build_symlinks_do_not_prevent_listing_visual_test_outputs() -> None:
    artifacts = [
        {
            "kind": "build",
            "id": {"targetCompleted": {"label": "//ui:node_modules"}},
            "file": {
                "name": "node_modules/@codemirror/language",
                "symlinkTargetPath": "../../../@codemirror+language/node_modules/@codemirror/language",
            },
        },
        *(
            {
                "kind": "test",
                "id": {"testResult": {"label": "//ui:visual"}},
                "file": {"name": f"test.outputs/{name}", "uri": f"bytestream://{name}"},
            }
            for name in ["test-outputs/records/scene.json", "test-outputs/files/screen.png"]
        ),
    ]

    def run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        selected = artifacts
        if "--kind" in words:
            kind = words[words.index("--kind") + 1]
            selected = [artifact for artifact in artifacts if artifact["kind"] == kind]
        return subprocess.CompletedProcess(command, 0, json.dumps(selected), "")

    listed = list_ci_artifacts(["invocation"], run=run)

    assert [(item.artifact.label, item.artifact.file.name, item.artifact.file.uri) for item in listed] == [
        ("//ui:visual", "test.outputs/test-outputs/records/scene.json", "bytestream://test-outputs/records/scene.json"),
        ("//ui:visual", "test.outputs/test-outputs/files/screen.png", "bytestream://test-outputs/files/screen.png"),
    ]


@pytest.mark.parametrize("name", ["test-outputs/records/scene.json", "test-outputs/files/screen.png"])
def test_visual_test_outputs_still_require_download_uris(name: str) -> None:
    artifacts = [{"id": {"testResult": {"label": "//ui:visual"}}, "file": {"name": f"test.outputs/{name}"}}]

    def run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    with pytest.raises(ValidationError, match="uri"):
        list_ci_artifacts(["invocation"], run=run)


def test_required_listing_does_not_hide_one_failed_invocation() -> None:
    artifact = {
        "id": {"testResult": {"label": "//ui:visual"}},
        "file": {"name": "test.outputs/test-outputs/records/a.json", "uri": "bytestream://record"},
    }

    def run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "failed" in command:
            return subprocess.CompletedProcess(command, 1, "", "listing unavailable")
        return subprocess.CompletedProcess(command, 0, json.dumps([artifact]), "")

    with pytest.raises(RuntimeError, match="failed: listing unavailable"):
        list_ci_artifacts(["successful", "failed"], run=run, require_all=True)

    assert len(list_ci_artifacts(["successful", "failed"], run=run)) == 1


if __name__ == "__main__":
    pytest_bazel.main()
