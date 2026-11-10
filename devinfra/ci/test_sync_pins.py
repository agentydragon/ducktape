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
    ("old_util", "old_ws", "old_gmail", "expected"),
    [
        (True, True, True, []),
        (False, True, True, ["ws imports util.agent_sandbox", "gmail-archiver imports util.fmt"]),
        (False, False, True, ["gmail-archiver imports util.fmt"]),
        (False, False, False, []),
    ],
)
def test_util_pin_waits_for_released_wheel_consumers(
    old_util: bool, old_ws: bool, old_gmail: bool, expected: list[str]
) -> None:
    util_files = {"util/agent_sandbox.py": "", "util/fmt.py": ""} if old_util else {}
    consumers = {
        "ws": _wheel({"devinfra/ws/cli.py": "from util.agent_sandbox import CLAIMS_PLURAL" if old_ws else ""}),
        "gmail-archiver": _wheel(
            {
                "gmail_archiver/main.py": "from util.fmt import format_truncation_suffix" if old_gmail else "",
                "gmail_archiver/planners/doordash.py": "",
            }
        ),
    }
    assert missing_wheel_imports(_wheel(util_files), consumers) == expected


@pytest.mark.parametrize("migrated_consumers", [False, True])
def test_pin_sync_never_publishes_incompatible_wheel_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_consumers: bool
) -> None:
    filenames = {name: f"{name}.whl" for name in ("ducktape-util", "ws", "gmail-archiver")}

    def release(name: str, revision: str) -> SimpleNamespace:
        return SimpleNamespace(
            tag_name=f"{name}-{revision * 7}",
            created_at=int(revision),
            draft=False,
            prerelease=False,
            target_commitish=revision,
        )

    old_util = release("ducktape-util", "1")
    new_util = release("ducktape-util", "2")
    old_ws = release("ws", "1")
    old_gmail = release("gmail-archiver", "1")
    latest_ws = release("ws", "2") if migrated_consumers else old_ws
    latest_gmail = release("gmail-archiver", "2") if migrated_consumers else old_gmail
    releases = [old_util, new_util, old_ws, old_gmail, latest_ws, latest_gmail]
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
                "ws": Pin(url=url(old_ws, "ws"), sha256="old"),
                "gmail-archiver": Pin(url=url(old_gmail, "gmail-archiver"), sha256="old"),
            }
        ).model_dump_json()
    )
    monkeypatch.setattr(sync_pins, "sources_path", lambda: source)
    wheels = {
        url(new_util, "ducktape-util"): _wheel({}),
        url(latest_ws, "ws"): _wheel(
            {"devinfra/ws/cli.py": "" if migrated_consumers else "from util.agent_sandbox import CLAIMS_PLURAL"}
        ),
        url(latest_gmail, "gmail-archiver"): _wheel(
            {
                "gmail_archiver/main.py": "" if migrated_consumers else "from util.fmt import format_truncation_suffix",
                "gmail_archiver/planners/doordash.py": "",
            }
        ),
    }
    monkeypatch.setattr(sync_pins, "download_wheel", lambda url: wheels[url])

    sync_pins.main()

    pins = Sources.model_validate_json(source.read_text()).pins
    assert pins["ducktape-util"].url == url(new_util if migrated_consumers else old_util, "ducktape-util")
    assert pins["ws"].url == url(latest_ws, "ws")
    assert pins["gmail-archiver"].url == url(latest_gmail, "gmail-archiver")


if __name__ == "__main__":
    pytest_bazel.main()
