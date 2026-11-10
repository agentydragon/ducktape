import base64
import dataclasses
import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_bazel
from github.GithubException import UnknownObjectException

from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.ci import sync_pins
from devinfra.ci.artifacts import Artifact, Pin, Sources
from devinfra.ci.pin_publication_test_support import SOURCE, normal, output, release as release_row, run
from devinfra.ci.plan_releases import content_hash, content_tag
from devinfra.ci.sync_pins import missing_wheel_imports

BUILD = str(invocation_id(run_id="200", attempt="1", role="build"))


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
    selected = {"ducktape-util": new_util, "ws": latest_ws, "gmail-archiver": latest_gmail}
    _configure(monkeypatch, tmp_path, filenames, selected)
    monkeypatch.setattr(
        sync_pins, "desired_tags", lambda *_args, **_kwargs: {name: r.tag_name for name, r in selected.items()}
    )

    def url(release: SimpleNamespace, name: str) -> str:
        return f"{sync_pins.BASE}/{release.tag_name}/{filenames[name]}"

    source = tmp_path / "pins.json"
    source.write_text(
        Sources(
            pins={
                "ducktape-util": Pin(url=url(old_util, "ducktape-util"), sha256=HASH),
                "ws": Pin(url=url(old_ws, "ws"), sha256=HASH),
                "gmail-archiver": Pin(url=url(old_gmail, "gmail-archiver"), sha256=HASH),
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

    sync_pins.main(["--source-commit", SOURCE])

    pins = Sources.model_validate_json(source.read_text()).pins
    assert pins["ducktape-util"].url == url(new_util if migrated_consumers else old_util, "ducktape-util")
    assert pins["ws"].url == url(latest_ws, "ws")
    assert pins["gmail-archiver"].url == url(latest_gmail, "gmail-archiver")


HASH = base64.b64encode(hashlib.sha256(b"stamped asset").digest()).decode()


def _configure(monkeypatch, tmp_path, filenames, selected):
    jobs: list[SimpleNamespace] = []
    current = run(jobs=lambda **_: jobs)
    repo = SimpleNamespace(
        get_branch=lambda _: SimpleNamespace(commit=SimpleNamespace(sha=SOURCE)),
        get_workflow=lambda _: SimpleNamespace(get_runs=lambda **_: [current]),
        get_release=lambda tag: next(r for r in selected.values() if r.tag_name == tag),
        compare=lambda _base, _head: SimpleNamespace(status="ahead"),
    )
    for name, published in selected.items():
        published.raw_data = {
            "assets": [{"name": filenames[name], "digest": "sha256:" + hashlib.sha256(b"stamped asset").hexdigest()}]
        }
    monkeypatch.setattr(sync_pins, "Github", lambda **_: SimpleNamespace(get_repo=lambda _: repo))
    monkeypatch.setattr(
        sync_pins, "ARTIFACTS", [Artifact(pkg=name, filename=filename) for name, filename in filenames.items()]
    )
    monkeypatch.setattr(
        sync_pins,
        "load_releases",
        lambda *_: [release_row(name, filenames=filename) for name, filename in filenames.items()],
    )
    monkeypatch.setattr(sync_pins, "url_sha256", lambda _: HASH)
    monkeypatch.setattr(sync_pins, "effective_rbe_image", lambda _: "registry/image@sha256:" + "a" * 64)
    monkeypatch.setattr(sync_pins, "read_build", {BUILD: normal()}.get)
    monkeypatch.setattr(sync_pins, "sources_path", lambda: tmp_path / "pins.json")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    return repo, current


@pytest.mark.parametrize("overall_success", [True, False])
def test_rollback_selects_existing_a_instead_of_newer_b_without_release_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overall_success: bool
) -> None:
    a = SimpleNamespace(tag_name="bbr-" + "a" * 12, target_commitish="feature-origin", draft=False, prerelease=False)
    repo, current = _configure(monkeypatch, tmp_path, {"bbr": "wheel.whl"}, {"bbr": a})
    # No Release bbr job: planner skipped existing A, despite unrelated failure.
    current.conclusion = "failure"
    monkeypatch.setattr(sync_pins, "read_build", {BUILD: dataclasses.replace(normal(), successful=overall_success)}.get)
    source = tmp_path / "pins.json"
    source.write_text(
        Sources(pins={"bbr": Pin(url=f"{sync_pins.BASE}/bbr-{'b' * 12}/wheel.whl", sha256=HASH)}).model_dump_json()
    )
    sync_pins.main(["--source-commit", SOURCE])
    result = Sources.model_validate_json(source.read_text())
    assert result.pins["bbr"].url == f"{sync_pins.BASE}/{a.tag_name}/wheel.whl"
    # A second (pin-only) CI run advertises A too: no rewrite/output/commit loop.
    before = source.stat().st_mtime_ns
    (tmp_path / "github-output").unlink()
    sync_pins.main(["--source-commit", SOURCE])
    assert source.stat().st_mtime_ns == before
    assert not (tmp_path / "github-output").exists()
    # A late older-source B run can never become eligible for this checkout.
    repo.get_workflow = lambda _: SimpleNamespace(get_runs=lambda **_: [run(source="1" * 40, identity=999)])
    sync_pins.main(["--source-commit", SOURCE])
    assert source.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "failure", ["missing", "draft", "digest", "missing-digest", "unavailable", "pending", "source", "stale"]
)
def test_unavailable_or_unverified_candidate_never_changes_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    a = SimpleNamespace(tag_name="bbr-" + "a" * 12, target_commitish="feature-origin", draft=False, prerelease=False)
    repo, current = _configure(monkeypatch, tmp_path, {"bbr": "wheel.whl"}, {"bbr": a})
    source = tmp_path / "pins.json"
    original = Sources(pins={"bbr": Pin(url="old", sha256=HASH)}).model_dump_json()
    source.write_text(original)
    if failure == "missing":
        repo.get_release = lambda _: (_ for _ in ()).throw(UnknownObjectException(404))
    elif failure == "draft":
        a.draft = True
    elif failure == "missing-digest":
        del a.raw_data["assets"][0]["digest"]
    elif failure == "digest":
        monkeypatch.setattr(sync_pins, "url_sha256", lambda _: base64.b64encode(b"x" * 32).decode())
    elif failure == "unavailable":
        monkeypatch.setattr(sync_pins, "url_sha256", lambda _: (_ for _ in ()).throw(httpx.ConnectError("unavailable")))
    elif failure == "pending":
        current.status = "in_progress"
    elif failure == "source":
        monkeypatch.setattr(sync_pins, "read_build", lambda _: None)
    elif failure == "stale":
        repo.get_branch = lambda _: SimpleNamespace(commit=SimpleNamespace(sha="new-devel"))
    sync_pins.main(["--source-commit", SOURCE])
    assert source.read_text() == original
    assert not (tmp_path / "github-output").exists()


@pytest.mark.parametrize("complete", [False, True])
def test_multi_asset_publication_updates_atomically_and_checks_stamped_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, complete: bool
) -> None:
    published = SimpleNamespace(tag_name="unused", draft=False, prerelease=False)
    _configure(monkeypatch, tmp_path, {"pair": "wheel.whl"}, {"pair": published})
    row = release_row("pair", "//pkg:wheel //pkg:zip", "wheel.whl extension.zip")
    monkeypatch.setattr(sync_pins, "load_releases", lambda *_: [row])
    monkeypatch.setattr(
        sync_pins,
        "ARTIFACTS",
        [
            Artifact(pkg="wheel", filename="wheel.whl", tag_pkg="pair"),
            Artifact(pkg="extension", filename="extension.zip", tag_pkg="pair"),
        ],
    )
    invocation = normal([output(), output("//pkg:zip", "extension.zip", "b" * 64)])
    digest = content_hash(row, invocation.by_label())
    assert digest is not None
    published.tag_name = content_tag("pair", digest)
    published.raw_data = {
        "assets": [{"name": "wheel.whl", "digest": "sha256:" + hashlib.sha256(b"stamped asset").hexdigest()}]
    }
    if complete:
        published.raw_data["assets"].append(
            {"name": "extension.zip", "digest": "sha256:" + hashlib.sha256(b"stamped asset").hexdigest()}
        )
    monkeypatch.setattr(sync_pins, "read_build", {BUILD: invocation}.get)
    source = tmp_path / "pins.json"
    original = Sources(pins={name: Pin(url="old", sha256=HASH) for name in ("wheel", "extension")}).model_dump_json()
    source.write_text(original)
    sync_pins.main(["--source-commit", SOURCE])
    pins = Sources.model_validate_json(source.read_text()).pins
    assert all((published.tag_name in pin.url) == complete for pin in pins.values())


def test_dry_run_verifies_candidate_without_writing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    published = SimpleNamespace(tag_name="bbr-" + "a" * 12, draft=False, prerelease=False)
    _configure(monkeypatch, tmp_path, {"bbr": "wheel.whl"}, {"bbr": published})
    source = tmp_path / "pins.json"
    original = Sources(pins={"bbr": Pin(url="old", sha256=HASH)}).model_dump_json()
    source.write_text(original)
    sync_pins.main(["--source-commit", SOURCE, "--dry-run"])
    assert source.read_text() == original
    assert not (tmp_path / "github-output").exists()


if __name__ == "__main__":
    pytest_bazel.main()
