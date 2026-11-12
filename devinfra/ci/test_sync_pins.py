import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_bazel

from devinfra.ci import sync_pins
from devinfra.ci.artifacts import Artifact, Pin, Sources
from devinfra.ci.sync_pins import missing_wheel_imports, release_is_on_devel


@dataclass
class FakeComparison:
    status: str


class FakeRepo:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.compared: list[tuple[str, str]] = []

    def compare(self, base: str, head: str) -> FakeComparison:
        self.compared.append((base, head))
        return FakeComparison(self.statuses[base])


def test_rejects_feature_release_and_caches_provenance() -> None:
    repo = FakeRepo({"feature": "diverged", "old-devel": "ahead"})
    reachable: dict[str, bool] = {}
    feature = SimpleNamespace(target_commitish="feature", tag_name="artifact-feature")
    old_devel = SimpleNamespace(target_commitish="old-devel", tag_name="artifact-old")

    assert not release_is_on_devel(repo, feature, "current-devel", reachable)
    assert release_is_on_devel(repo, old_devel, "current-devel", reachable)
    assert not release_is_on_devel(repo, feature, "current-devel", reachable)
    assert repo.compared == [("feature", "current-devel"), ("old-devel", "current-devel")]


def _wheel(files: dict[str, str]) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return result.getvalue()


@pytest.mark.parametrize(
    ("util_files", "hook_source", "expected"),
    [
        ({"util/otel.py": ""}, "from util.otel import JsonlSpanExporter", []),
        ({"util/otel/__init__.py": ""}, "from util.otel import JsonlSpanExporter", []),
        ({}, "from util.otel import JsonlSpanExporter", ["ducktape-git-hooks imports util.otel"]),
        ({}, "from util import otel", ["ducktape-git-hooks imports util.otel"]),
        ({}, "import logging", []),
    ],
)
def test_util_pin_waits_for_released_wheel_consumers(
    util_files: dict[str, str], hook_source: str, expected: list[str]
) -> None:
    hooks = _wheel({"devinfra/precommit/git_hook.py": hook_source})
    assert missing_wheel_imports(_wheel(util_files), hooks) == expected


@pytest.mark.parametrize(
    ("new_util", "new_hooks", "util_has_otel", "hooks_import_otel", "hooks_available", "advance"),
    [
        (True, False, False, True, True, False),  # Existing hooks block removal of util.otel.
        (False, True, False, True, True, False),  # New hooks cannot add an unavailable import.
        (True, True, True, True, True, True),  # Both pins advance when the proposed pair is compatible.
        (True, True, False, False, True, True),  # A migrated hooks wheel allows util.otel removal.
        (True, False, True, False, False, False),  # Missing hooks wheel blocks the util update.
    ],
)
def test_pin_sync_never_publishes_incompatible_wheel_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    new_util: bool,
    new_hooks: bool,
    util_has_otel: bool,
    hooks_import_otel: bool,
    hooks_available: bool,
    advance: bool,
) -> None:
    # Hooks precede util in the real artifact inventory.
    filenames = {name: f"{name}.whl" for name in ("ducktape-git-hooks", "ducktape-util", "unrelated")}

    def release(name: str, revision: str) -> SimpleNamespace:
        return SimpleNamespace(
            tag_name=f"{name}-{revision * 7}",
            created_at=int(revision),
            draft=False,
            prerelease=False,
            target_commitish=revision,
        )

    old_util = release("ducktape-util", "1")
    old_hooks = release("ducktape-git-hooks", "1")
    latest_util = release("ducktape-util", "2") if new_util else old_util
    latest_hooks = release("ducktape-git-hooks", "2") if new_hooks else old_hooks
    old_unrelated = release("unrelated", "1")
    latest_unrelated = release("unrelated", "2")
    releases = [old_util, latest_util, old_unrelated, latest_unrelated]
    # A missing release listing must still check the currently pinned hooks wheel.
    if new_hooks:
        releases.extend([old_hooks, latest_hooks])
    repo = SimpleNamespace(
        get_releases=lambda: releases,
        get_branch=lambda _: SimpleNamespace(commit=SimpleNamespace(sha="devel")),
        compare=lambda _base, _head: SimpleNamespace(status="ahead"),
    )
    monkeypatch.setattr(sync_pins, "Github", lambda **_: SimpleNamespace(get_repo=lambda _: repo))
    monkeypatch.setattr(
        sync_pins, "ARTIFACTS", [Artifact(pkg=name, filename=filename) for name, filename in filenames.items()]
    )
    monkeypatch.setattr(sync_pins, "url_sha256", lambda _: "sha256")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))

    def url(release: SimpleNamespace, name: str) -> str:
        return f"{sync_pins.BASE}/{release.tag_name}/{filenames[name]}"

    source = tmp_path / "pins.json"
    source.write_text(
        Sources(
            pins={
                "ducktape-util": Pin(url=url(old_util, "ducktape-util"), sha256="old"),
                **(
                    {"ducktape-git-hooks": Pin(url=url(old_hooks, "ducktape-git-hooks"), sha256="old")}
                    if hooks_available
                    else {}
                ),
                "unrelated": Pin(url=url(old_unrelated, "unrelated"), sha256="old"),
            }
        ).model_dump_json()
    )
    monkeypatch.setattr(sync_pins, "sources_path", lambda: source)
    wheels = {
        url(latest_util, "ducktape-util"): _wheel({"util/otel.py": ""} if util_has_otel else {}),
        url(latest_hooks, "ducktape-git-hooks"): _wheel(
            {"devinfra/precommit/git_hook.py": "from util.otel import JsonlSpanExporter" if hooks_import_otel else ""}
        ),
    }
    monkeypatch.setattr(sync_pins, "download_wheel", lambda url: wheels[url])

    sync_pins.main()

    pins = Sources.model_validate_json(source.read_text()).pins
    assert pins["ducktape-util"].url == url(latest_util if advance else old_util, "ducktape-util")
    if hooks_available:
        assert pins["ducktape-git-hooks"].url == url(latest_hooks if advance else old_hooks, "ducktape-git-hooks")
    else:
        assert "ducktape-git-hooks" not in pins
    assert pins["unrelated"].url == url(latest_unrelated, "unrelated")


if __name__ == "__main__":
    pytest_bazel.main()
