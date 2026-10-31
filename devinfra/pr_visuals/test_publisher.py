from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import pytest
import pytest_bazel
from botocore.exceptions import ClientError
from PIL import Image

from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.pr_visuals.publisher import (
    COMMENT_BUDGET,
    COMMENT_MARKER,
    BaselinePointer,
    ClassificationCounts,
    DownloadedVisualTest,
    ReviewAsset,
    ReviewBundleMetadata,
    ReviewTest,
    build_bundle,
    diff_check,
    download_visual_tests,
    error_comment_body,
    find_test_invocations,
    list_ci_artifacts,
    list_ci_failures,
    main,
    no_visual_comment_body,
    refresh_stale_pull_request_comment,
    success_comment_body,
    target_slug,
    upload_bundle,
    wait_for_test_invocations,
    write_baseline_pointers,
)
from devinfra.pr_visuals.pull_request import PullRequestRef
from util.visual_review import VisualReviewManifest


def _png(path: Path, color: tuple[int, int, int, int] = (10, 20, 30, 255), size: tuple[int, int] = (8, 8)) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


class FakeBaselineSource:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _manifest(title: str, *assets: str) -> bytes:
    return json.dumps(
        {
            "schema": "ducktape.visual-review.v1",
            "title": title,
            "assets": [{"path": asset, "label": asset.removesuffix(".png")} for asset in assets],
        }
    ).encode()


def _listing(
    artifacts: list[dict[str, object]], blobs: dict[str, bytes] | None = None
) -> Callable[..., subprocess.CompletedProcess[str]]:
    blobs = blobs or {}

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words and words[words.index("blob") + 1] == "download":
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            for item in manifest:
                Path(item["destination"]).write_bytes(blobs[item["uri"]])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    return fake_run


def _listing_per_invocation(
    by_invocation: dict[str, list[dict[str, object]]], blobs: dict[str, bytes] | None = None
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`bbapi artifact list <invocation> --kind test --json`, answered differently per invocation."""
    blobs = blobs or {}

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "blob" in words and words[words.index("blob") + 1] == "download":
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            for item in manifest:
                Path(item["destination"]).write_bytes(blobs[item["uri"]])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(by_invocation[words[3]]), "")

    return fake_run


def test_the_full_sweep_wins_over_an_affected_set_run_at_the_same_commit() -> None:
    """The case that made #4927 insufficient: one commit, several CI runs. Only the
    `//...` sweep carries the visual manifests; an affected-set run at the same commit
    is complete and real and has none. Picking by commit must prefer the sweep."""
    command_seen: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        command_seen.append(words)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "invocation": [
                        {"invocationId": "affected-set", "pattern": ["//cluster/k8s:cluster_all_files"]},
                        {"invocationId": "full-sweep", "pattern": ["//..."]},
                    ]
                }
            ),
            "",
        )

    found = find_test_invocations(
        run_id="33066954750", run_attempt="1", commit_sha="5ee3b732cc68b03ef86a1b6e95598f54d796e8f2", run=fake_run
    )
    assert found == ["full-sweep"]
    assert command_seen == [
        [
            "bbapi",
            "--timeout",
            "30s",
            "invocation",
            "list",
            "--repo",
            "https://github.com/agentydragon/ducktape",
            "--commit",
            "5ee3b732cc68b03ef86a1b6e95598f54d796e8f2",
            "--command",
            "test",
            "--role",
            "CI",
            "--count",
            "25",
            "--json",
        ]
    ]


def test_an_invocation_that_never_existed_is_not_a_failure() -> None:
    """A run cancelled before Bazel started names two invocations BuildBuddy has never
    seen, because the IDs are assigned up front. That is a normal empty result — raising
    would turn the quietest case into a red job."""
    calls = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="Error: HTTP 500: rpc error: code = NotFound desc = invocation not found",
    )
    assert list_ci_artifacts(["absent-a", "absent-b"], run=lambda *_a, **_k: calls) == []


def test_a_genuine_query_failure_still_raises() -> None:
    """A transport or auth failure must not read as 'this run had no visuals' — that
    would publish an empty bundle over a commit that really did render something."""
    broken = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Error: HTTP 503: upstream timeout")
    with pytest.raises(RuntimeError):
        list_ci_artifacts(["a", "b"], run=lambda *_a, **_k: broken)


def test_late_invocation_artifacts_are_read_only_after_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    candidate = _single_asset_test(tmp_path / "candidate")
    states = iter(["PARTIAL_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS"])
    complete = False
    listed_after_completion: list[bool] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "invocation" in words and words[words.index("invocation") + 1] == "list":
            return subprocess.CompletedProcess(
                command, 0, '{"invocation":[{"invocationId":"late-sweep","pattern":["//..."]}]}', ""
            )
        nonlocal complete
        state = next(states)
        complete = state == "COMPLETE_INVOCATION_STATUS"
        return subprocess.CompletedProcess(command, 0, json.dumps({"invocation": [{"invocationStatus": state}]}), "")

    def download(_invocations: list[str], _destination: Path, **_kwargs: object) -> list[DownloadedVisualTest]:
        listed_after_completion.append(complete)
        return [candidate] if complete else []

    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", fake_run)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", download)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.INVOCATION_POLL_SECONDS", 0)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.boto3.client", lambda *_a, **_k: EmptyBucket())
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upload_bundle", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.find_reviewable_pull_request", lambda **_k: PullRequestRef(8733, "c" * 40)
    )

    main()

    assert listed_after_completion == [True]
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "success"]


def test_incomplete_invocation_cannot_advance_a_devel_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr("sys.argv", _publisher_argv(tmp_path / "work"))
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, '{"invocation":[]}', ""),
    )
    monkeypatch.setattr("devinfra.pr_visuals.publisher.wait_for_test_invocations", lambda *_a, **_k: "incomplete")
    _forbid(monkeypatch, "download_visual_tests", "upload_bundle", "write_baseline_pointers")

    with pytest.raises(RuntimeError, match="did not complete"):
        main()

    assert [check["conclusion"] for check in publisher_run.checks] == ["failure"]


@pytest.mark.parametrize("first", [None, "PARTIAL_INVOCATION_STATUS", "DISCONNECTED_INVOCATION_STATUS"])
def test_wait_for_invocations_uses_terminal_state(first: str | None) -> None:
    statuses = iter([first, "COMPLETE_INVOCATION_STATUS"])
    waits: list[float] = []
    assert (
        wait_for_test_invocations(
            ["known"],
            run=lambda command, **_kwargs: subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"invocation": [{"invocationStatus": state}] if (state := next(statuses)) else []}),
                "",
            ),
            monotonic=lambda: 0,
            wait=waits.append,
        )
        == "complete"
    )
    assert waits == [2]


def test_wait_for_invocations_has_a_bounded_deadline() -> None:
    clock = iter([0, 300])
    assert (
        wait_for_test_invocations(
            ["still-running"],
            run=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, '{"invocation":[{"invocationStatus":"PARTIAL_INVOCATION_STATUS"}]}', ""
            ),
            monotonic=lambda: next(clock),
            wait=lambda _seconds: pytest.fail("the deadline elapsed"),
        )
        == "incomplete"
    )


def test_failed_workflow_does_not_treat_missing_invocations_as_no_tests() -> None:
    states = iter([None, "PARTIAL_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS"])
    waits: list[float] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        selected = words[words.index("status") + 1]
        if selected == "build":
            return subprocess.CompletedProcess(command, 0, "{}", "")
        state = next(states)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"invocation": [{"invocationStatus": state}] if state else []}), ""
        )

    assert (
        wait_for_test_invocations(
            ["test"],
            allow_build_only=True,
            build_invocation_id="build",
            run=fake_run,
            monotonic=lambda: 0,
            wait=waits.append,
        )
        == "complete"
    )
    assert waits == [2, 2]


def test_successful_build_only_run_has_no_visuals_without_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr("sys.argv", _publisher_argv(tmp_path / "work"))

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "invocation" in words and words[words.index("invocation") + 1] == "list":
            return subprocess.CompletedProcess(command, 0, '{"invocation":[]}', "")
        selector = words[words.index("status") + 1]
        build_id = str(invocation_id(run_id="33060467222", attempt="1", role="build"))
        return subprocess.CompletedProcess(
            command,
            0,
            '{"invocation":[{"invocationStatus":"COMPLETE_INVOCATION_STATUS"}]}'
            if selector == build_id
            else '{"invocation":[]}',
            "",
        )

    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", fake_run)
    _forbid(monkeypatch, "download_visual_tests", "upload_bundle", "write_baseline_pointers")

    main()

    assert [check["conclusion"] for check in publisher_run.checks] == ["neutral"]


def test_download_visual_tests_groups_manifests_by_executed_target(tmp_path: Path) -> None:
    artifacts = [
        {
            "id": {"testResult": {"label": "//haku/console/frontend:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://manifest-haku"},
        },
        {
            "id": {"testResult": {"label": "//haku/console/frontend:screenshots"}},
            "file": {"name": "test.outputs/preview.png", "uri": "bytestream://preview-haku"},
        },
        {
            "id": {"testResult": {"label": "//aiquota/gnome:test_render"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://manifest-aiquota"},
        },
        {
            "id": {"testResult": {"label": "//aiquota/gnome:test_render"}},
            "file": {"name": "test.outputs/hot.png", "uri": "bytestream://hot-aiquota"},
        },
    ]
    commands: list[list[str | Path]] = []
    blob_values = {
        "bytestream://manifest-haku": _manifest("Haku Console", "preview.png"),
        "bytestream://preview-haku": b"png",
        "bytestream://manifest-aiquota": _manifest("AI quota", "hot.png"),
        "bytestream://hot-aiquota": b"png",
    }
    download_blobs = _listing(artifacts, blob_values)

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        words = [str(part) for part in command]
        if "blob" in words and words[words.index("blob") + 1] == "download":
            return download_blobs(command, **_kwargs)
        if words[3] == "missing":
            return subprocess.CompletedProcess(command, 1, "", "invocation not found")
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    tests = download_visual_tests(["missing", "real"], tmp_path / "tests", run=fake_run)

    assert [test.target_label for test in tests] == [
        "//aiquota/gnome:test_render",
        "//haku/console/frontend:screenshots",
    ]
    assert (tests[0].directory / "hot.png").read_bytes() == b"png"
    assert commands[1] == [Path("bbapi"), "artifact", "list", "real", "--kind", "test", "--json"]
    assert sum("blob" in [str(part) for part in command] for command in commands) == 2


def test_bbapi_lists_once_and_downloads_each_blob_wave_from_one_manifest(tmp_path: Path) -> None:
    """The listing resolves each URI once; manifests and images then download in two
    bulk calls, without refetching the invocation's event stream per file."""
    artifacts = [
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://manifest"},
        },
        *(
            {
                "id": {"testResult": {"label": "//ui:screenshots"}},
                "file": {"name": f"test.outputs/shot-{index}.png", "uri": f"bytestream://shot-{index}"},
            }
            for index in range(12)
        ),
    ]
    shots = [f"shot-{index}.png" for index in range(12)]
    calls: list[list[str | Path]] = []
    manifests: list[list[dict[str, str]]] = []
    blobs = {"bytestream://manifest": _manifest("UI", *shots)} | {
        f"bytestream://shot-{index}": b"png" for index in range(12)
    }

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        words = [str(part) for part in command]
        if "blob" in words and words[words.index("blob") + 1] == "download":
            manifest = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
            manifests.append(manifest)
            for item in manifest:
                Path(item["destination"]).write_bytes(blobs[item["uri"]])
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    tests = download_visual_tests(["invocation"], tmp_path / "tests", run=fake_run)

    assert len(tests[0].manifest.assets) == 12
    assert [call for call in calls if [str(part) for part in call][1:3] == ["artifact", "list"]] == [
        [Path("bbapi"), "artifact", "list", "invocation", "--kind", "test", "--json"]
    ]
    assert len(manifests) == 2
    assert len(manifests[0]) == 1
    assert len(manifests[1]) == 12
    assert all(item["invocation_id"] == "invocation" for manifest in manifests for item in manifest)
    slug = target_slug("//ui:screenshots")
    assert manifests[0][0]["destination"] == str(tmp_path / "tests" / ".manifests" / slug / "0.json")
    assert [item["destination"] for item in manifests[1]] == [
        str(tmp_path / "tests" / slug / f"shot-{index}.png") for index in range(12)
    ]


def test_download_visual_tests_rejects_missing_declared_asset(tmp_path: Path) -> None:
    artifacts = [
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://manifest"},
        }
    ]

    with pytest.raises(ValueError, match=r"references missing artifact missing\.png"):
        download_visual_tests(
            ["invocation"],
            tmp_path / "tests",
            run=_listing(artifacts, {"bytestream://manifest": _manifest("UI", "missing.png")}),
        )


def test_download_visual_tests_deduplicates_equivalent_manifests(tmp_path: Path) -> None:
    artifacts = [
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://manifest-first"},
        },
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://manifest-retry"},
        },
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/screen.png", "uri": "bytestream://screen"},
        },
    ]
    manifest = _manifest("UI", "screen.png")

    tests = download_visual_tests(
        ["invocation"],
        tmp_path / "tests",
        run=_listing(
            artifacts,
            {
                "bytestream://manifest-first": manifest,
                "bytestream://manifest-retry": manifest,
                "bytestream://screen": b"png",
            },
        ),
    )

    assert len(tests) == 1
    assert (tests[0].directory / "screen.png").read_bytes() == b"png"


def test_download_visual_tests_unions_a_sharded_targets_manifests(tmp_path: Path) -> None:
    """One `shard_count` target, one manifest per shard, each naming only its own scenes.

    The target's review is their union — the shards are one test, not competing answers.
    """
    artifacts = [
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://shard-0"},
        },
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://shard-1"},
        },
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/list.png", "uri": "bytestream://list"},
        },
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/detail.png", "uri": "bytestream://detail"},
        },
    ]

    tests = download_visual_tests(
        ["invocation"],
        tmp_path / "tests",
        run=_listing(
            artifacts,
            {
                "bytestream://shard-0": _manifest("UI", "list.png"),
                "bytestream://shard-1": _manifest("UI", "detail.png"),
                "bytestream://list": b"list-png",
                "bytestream://detail": b"detail-png",
            },
        ),
    )

    assert len(tests) == 1
    assert {asset.path for asset in tests[0].manifest.assets} == {"list.png", "detail.png"}
    assert (tests[0].directory / "list.png").read_bytes() == b"list-png"
    assert (tests[0].directory / "detail.png").read_bytes() == b"detail-png"


def test_download_visual_tests_rejects_a_sweep_and_an_affected_set_run_disagreeing(tmp_path: Path) -> None:
    """Shards union; whole rosters from separate invocations must agree.

    A full `//...` sweep and an affected-set run at one commit publish different rosters for the
    same target. Unioning them yields a review that looks complete while taking each shot from
    whichever run was listed first, so it stays an error -- which is what find_test_invocations'
    "the two are not combined" relies on.
    """
    by_invocation = {
        "sweep": [
            {
                "id": {"testResult": {"label": "//ui:visual"}},
                "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://sweep-manifest"},
            },
            {
                "id": {"testResult": {"label": "//ui:visual"}},
                "file": {"name": "test.outputs/list.png", "uri": "bytestream://list"},
            },
            {
                "id": {"testResult": {"label": "//ui:visual"}},
                "file": {"name": "test.outputs/detail.png", "uri": "bytestream://detail"},
            },
        ],
        "affected": [
            {
                "id": {"testResult": {"label": "//ui:visual"}},
                "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://affected-manifest"},
            },
            {
                "id": {"testResult": {"label": "//ui:visual"}},
                "file": {"name": "test.outputs/list.png", "uri": "bytestream://list"},
            },
        ],
    }

    with pytest.raises(ValueError, match="different visual-review rosters"):
        download_visual_tests(
            ["sweep", "affected"],
            tmp_path / "tests",
            run=_listing_per_invocation(
                by_invocation,
                {
                    "bytestream://sweep-manifest": _manifest("UI", "list.png", "detail.png"),
                    "bytestream://affected-manifest": _manifest("UI", "list.png"),
                    "bytestream://list": b"list-png",
                    "bytestream://detail": b"detail-png",
                },
            ),
        )


def test_download_visual_tests_unions_shards_across_a_retried_invocation(tmp_path: Path) -> None:
    """A sharded target listed through two invocations: each unions to the same roster, so it publishes."""
    shards = [
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://shard-0"},
        },
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "bytestream://shard-1"},
        },
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/list.png", "uri": "bytestream://list"},
        },
        {
            "id": {"testResult": {"label": "//ui:visual"}},
            "file": {"name": "test.outputs/detail.png", "uri": "bytestream://detail"},
        },
    ]

    tests = download_visual_tests(
        ["primary", "child"],
        tmp_path / "tests",
        run=_listing_per_invocation(
            {"primary": shards, "child": shards},
            {
                "bytestream://shard-0": _manifest("UI", "list.png"),
                "bytestream://shard-1": _manifest("UI", "detail.png"),
                "bytestream://list": b"list-png",
                "bytestream://detail": b"detail-png",
            },
        ),
    )

    assert len(tests) == 1
    assert {asset.path for asset in tests[0].manifest.assets} == {"list.png", "detail.png"}


def test_download_visual_tests_rejects_conflicting_titles(tmp_path: Path) -> None:
    artifacts = [
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "manifest-one"},
        },
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "manifest-two"},
        },
    ]

    with pytest.raises(ValueError, match="conflicting visual-review titles"):
        download_visual_tests(
            ["invocation"],
            tmp_path / "tests",
            run=_listing(
                artifacts,
                {"manifest-one": _manifest("UI 1", "screen-1.png"), "manifest-two": _manifest("UI 2", "screen-2.png")},
            ),
        )


def test_download_visual_tests_rejects_one_asset_described_two_ways(tmp_path: Path) -> None:
    """Union resolves disjoint and identical manifests; it cannot resolve disagreement."""
    artifacts = [
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "manifest-one"},
        },
        {
            "id": {"testResult": {"label": "//ui:screenshots"}},
            "file": {"name": "test.outputs/visual-review.json", "uri": "manifest-two"},
        },
    ]
    relabelled = json.dumps(
        {
            "schema": "ducktape.visual-review.v1",
            "title": "UI",
            "assets": [{"path": "screen.png", "label": "a different label"}],
        }
    ).encode()

    with pytest.raises(ValueError, match=r"conflicting visual-review entries for screen\.png"):
        download_visual_tests(
            ["invocation"],
            tmp_path / "tests",
            run=_listing(artifacts, {"manifest-one": _manifest("UI", "screen.png"), "manifest-two": relabelled}),
        )


def test_download_visual_tests_treats_null_artifact_list_as_empty(tmp_path: Path) -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "null", "")

    assert download_visual_tests(["invocation"], tmp_path / "tests", run=fake_run) == []


def test_list_ci_failures_is_best_effort_and_deduplicates_labels() -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[2] == "missing":
            return subprocess.CompletedProcess(command, 1, "", "not found")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "targetGroups": [
                        {
                            "targets": [
                                {"metadata": {"label": "//z:test"}, "status": "FAILED"},
                                {"metadata": {"label": "//a:test"}, "status": "FAILED"},
                                {"metadata": {"label": "//ok:test"}, "status": "PASSED"},
                            ]
                        }
                    ]
                }
            ),
            "",
        )

    assert list_ci_failures(["missing", "invocation", "invocation"], run=fake_run) == ["//a:test", "//z:test"]


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

    assert bundle == tmp_path / "site" / "reviews" / sha
    assert "//example:visuals" in (bundle / "index.html").read_text()
    target_page = bundle / "tests" / target_slug("//example:visuals") / "index.html"
    assert 'id="screen.png"' in target_page.read_text()
    digest = hashlib.sha256(b"png").hexdigest()
    assert (tmp_path / "site" / "assets" / f"{digest}.png").read_bytes() == b"png"
    assert f"../../../../assets/{digest}.png" in target_page.read_text()
    assert not list(bundle.rglob("*.png"))


def test_build_bundle_rejects_abbreviated_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        build_bundle([], tmp_path, commit_sha="0123456", repository="repo")


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
    assert "https://visuals/commits/sha/index.html" in success
    assert "tests/example-visuals-" in success
    assert "missing artifact screen.png" in failure

    warning = success_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        url="https://visuals/commits/sha/",
        review_tests=review_tests,
        ci_conclusion="failure",
        ci_failures=["//haku/console:x_test"],
    )
    assert "//haku/console:x_test" in warning

    no_visuals = no_visual_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        ci_conclusion="failure",
        ci_failures=["//haku/console:x_test"],
        details_url="https://github/actions/runs/1",
    )
    assert "https://github/actions/runs/1" in no_visuals


HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPOSITORY = "agentydragon/ducktape"


def _success_comment(commit_sha: str) -> str:
    return success_comment_body(
        repository=REPOSITORY, commit_sha=commit_sha, url="https://visuals/commits/sha/", review_tests=[]
    )


# Each existing body comes from the function that writes it, so this pins that the refresh
# recognizes what the publisher actually posts, whatever its wording.
@pytest.mark.parametrize(
    ("existing_body", "expected_edit"),
    [
        pytest.param(
            error_comment_body(repository=REPOSITORY, commit_sha=HEAD_SHA, error=ValueError("boom")),
            True,
            id="failure-of-this-commit",
        ),
        pytest.param(_success_comment(OTHER_SHA), True, id="success-of-another-commit"),
        pytest.param(_success_comment(HEAD_SHA), False, id="success-of-this-commit"),
        pytest.param(
            no_visual_comment_body(
                repository=REPOSITORY, commit_sha=HEAD_SHA, ci_conclusion="failure", ci_failures=[], details_url=None
            ),
            True,
            id="no-artifacts-for-this-commit",
        ),
        pytest.param(None, False, id="no-comment"),
    ],
)
def test_refresh_stale_pull_request_comment(
    monkeypatch: pytest.MonkeyPatch, existing_body: str | None, expected_edit: bool
) -> None:
    edits: list[str] = []
    created: list[str] = []

    class FakeComment:
        body = existing_body
        user = type("User", (), {"type": "Bot"})()

        def edit(self, body: str) -> None:
            edits.append(body)

    class FakeIssue:
        def get_comments(self) -> list[FakeComment]:
            return [] if existing_body is None else [FakeComment()]

        def create_comment(self, body: str) -> None:
            created.append(body)

    class FakeGithub:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeGithub:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get_repo(self, _repository: str) -> FakeGithub:
            return self

        def get_issue(self, _pull_request: int) -> FakeIssue:
            return FakeIssue()

    monkeypatch.setattr("devinfra.pr_visuals.publisher.Github", FakeGithub)
    refresh_stale_pull_request_comment(
        repository=REPOSITORY, pull_request=4594, commit_sha=HEAD_SHA, body="replacement", token="token"
    )

    assert edits == (["replacement"] if expected_edit else [])
    assert created == []


PR_HEAD_REF = "pr-owner:topic"


def _publisher_argv(work_dir: Path, *options: str) -> list[str]:
    return [
        "publisher",
        "--ci-run-id",
        "33060467222",
        "--ci-run-attempt",
        "1",
        "--work-dir",
        str(work_dir),
        "--sha",
        HEAD_SHA,
        "--repository",
        "agentydragon/ducktape",
        "--endpoint",
        "https://s3.example",
        "--bucket",
        "pr-visuals",
        "--public-base-url",
        "https://s3.example/pr-visuals",
        *options,
    ]


def _forbid(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Fail the test if `publisher.main` reaches any of these."""

    def forbidden(name: str) -> Callable[..., NoReturn]:
        def fail(*_args: object, **_kwargs: object) -> NoReturn:
            pytest.fail(f"the publisher must not reach {name}")

        return fail

    for name in names:
        monkeypatch.setattr(f"devinfra.pr_visuals.publisher.{name}", forbidden(name))


def test_a_superseded_run_publishes_its_bundle_but_leaves_the_comment_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancellation kills the workflow, not the Bazel invocation, so a superseded run
    often holds a complete set of manifests — those get published, since the commit
    bundle is immutable and is what lets a PR on this commit resolve an exact baseline.
    It still says nothing: the comment is a singleton, and speaking here would replace
    the previous run's real review with a warning about an abandoned build. Pointers are
    left alone because they are mutable and unordered across concurrent publishes."""
    checks: list[dict[str, object]] = []
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upsert_check_run", lambda **kwargs: checks.append(kwargs))
    _forbid(
        monkeypatch,
        "find_reviewable_pull_request",
        "upsert_pull_request_comment",
        "refresh_stale_pull_request_comment",
        "write_baseline_pointers",
    )
    downloaded: list[list[str]] = []

    def record(invocations: list[str], _destination: Path, **_kwargs: object) -> list[object]:
        downloaded.append(invocations)
        return []

    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", record)
    # BuildBuddy holds nothing under this commit — a PR run records the merge SHA — so
    # the lookup falls through to the IDs derived from the run.
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, '{"invocation": []}', ""),
    )
    monkeypatch.setattr("devinfra.pr_visuals.publisher.wait_for_test_invocations", lambda *_a, **_k: "incomplete")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        "sys.argv",
        _publisher_argv(tmp_path / "work", "--ci-conclusion", "cancelled", "--pull-request-head", PR_HEAD_REF),
    )

    main()

    assert downloaded == [
        [
            str(invocation_id(run_id="33060467222", attempt="1", role="test")),
            str(invocation_id(run_id="33060467222", attempt="1", role="build")),
        ]
    ], "a superseded run must still look for the artifacts its Bazel invocation left behind"
    assert len(checks) == 1, "the announced in-progress check must still be terminated"
    assert checks[0]["conclusion"] == "neutral"
    assert checks[0]["commit_sha"] == HEAD_SHA


PR_CHECK_ID = "pr-visual-review:33060467222"


@dataclass
class PublisherRun:
    checks: list[dict[str, object]] = field(default_factory=list)
    comments: list[dict[str, object]] = field(default_factory=list)


@pytest.fixture
def publisher_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PublisherRun:
    """`main()` as the publish workflow starts it for a PR's CI run, recording its GitHub writes.

    The run's event is not trusted to name its pull request, so the publisher is handed the run's head ref
    instead. A test of another kind of run replaces `sys.argv`.
    """
    run = PublisherRun()
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upsert_check_run", lambda **kwargs: run.checks.append(kwargs))
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.upsert_pull_request_comment", lambda **kwargs: run.comments.append(kwargs)
    )

    # The PR head is absent from invocation search (CI records the merge SHA), but
    # its derived test invocation is complete when publication begins.
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        if "invocation" in words and words[words.index("invocation") + 1] == "list":
            output = '{"invocation": []}'
        elif "status" in words:
            output = '{"invocation":[{"invocationStatus":"COMPLETE_INVOCATION_STATUS"}]}'
        elif "artifact" in words:
            output = "[]"
        else:
            raise AssertionError(f"unexpected command {words}")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.subprocess.run", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        _publisher_argv(tmp_path / "work", "--pull-request-head", PR_HEAD_REF, "--check-external-id", PR_CHECK_ID),
    )
    return run


class EmptyBucket:
    """An S3 bucket nothing was ever published to, remembering which keys it was asked for."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def get_object(self, **kwargs: str) -> NoReturn:
        self.requested.append(kwargs["Key"])
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


def _publish_one_visual_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[DownloadedVisualTest, EmptyBucket]:
    """The run's Bazel invocation holds one visual target, and the bucket holds nothing yet."""
    candidate = _single_asset_test(tmp_path / "candidate")
    bucket = EmptyBucket()
    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr("devinfra.pr_visuals.publisher.boto3.client", lambda *_args, **_kwargs: bucket)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.upload_bundle", lambda *_args, **_kwargs: None)
    return candidate, bucket


def test_a_pr_run_publishes_to_the_pr_its_head_ref_finds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    """The PR and its base commit come from the lookup, never from the run's event. The run then comments
    on that PR, compares against that base and — being a PR run — leaves the devel baseline pointers alone."""
    base_sha = "c" * 40
    lookups: list[dict[str, str]] = []

    def find(**kwargs: str) -> PullRequestRef:
        lookups.append(kwargs)
        return PullRequestRef(number=8733, base_sha=base_sha)

    candidate, bucket = _publish_one_visual_target(monkeypatch, tmp_path)
    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", find)
    _forbid(monkeypatch, "write_baseline_pointers")

    main()

    assert [(lookup["head"], lookup["head_sha"]) for lookup in lookups] == [(PR_HEAD_REF, HEAD_SHA)] * 2
    assert [comment["pull_request"] for comment in publisher_run.comments] == [8733]
    assert bucket.requested[:2] == [
        f"reviews/{base_sha}/tests/{candidate.slug}/metadata.json",
        f"commits/{base_sha}/tests/{candidate.slug}/metadata.json",
    ]
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "success"]


@pytest.mark.parametrize("current", [None, PullRequestRef(number=9999, base_sha="c" * 40)])
def test_a_pr_that_changes_during_publication_does_not_get_a_stale_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun, current: PullRequestRef | None
) -> None:
    _publish_one_visual_target(monkeypatch, tmp_path)
    lookups = iter([PullRequestRef(number=8733, base_sha="c" * 40), current])
    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", lambda **_kwargs: next(lookups))
    _forbid(monkeypatch, "write_baseline_pointers")

    main()

    assert publisher_run.comments == []
    assert [check["conclusion"] for check in publisher_run.checks] == ["success", "success"]


def test_a_devel_push_run_advances_the_baseline_pointers_and_looks_up_no_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, publisher_run: PublisherRun
) -> None:
    candidate, _ = _publish_one_visual_target(monkeypatch, tmp_path)
    pointers: list[list[str]] = []
    monkeypatch.setattr(
        "devinfra.pr_visuals.publisher.write_baseline_pointers", lambda slugs, **_kwargs: pointers.append(slugs)
    )
    _forbid(monkeypatch, "find_reviewable_pull_request")
    monkeypatch.setattr("sys.argv", _publisher_argv(tmp_path / "work"))

    main()

    assert pointers == [[candidate.slug]]
    assert publisher_run.comments == []
    assert [check["conclusion"] for check in publisher_run.checks] == ["success"]


def test_a_pr_run_whose_commit_is_no_longer_a_pr_head_only_closes_its_check(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", lambda **_kwargs: None)
    _forbid(monkeypatch, "download_visual_tests", "refresh_stale_pull_request_comment", "write_baseline_pointers")

    main()

    assert [(check["conclusion"], check["external_id"]) for check in publisher_run.checks] == [("neutral", PR_CHECK_ID)]
    assert publisher_run.comments == []


def test_a_failed_pr_lookup_fails_the_check_instead_of_leaving_it_in_progress(
    monkeypatch: pytest.MonkeyPatch, publisher_run: PublisherRun
) -> None:
    def ambiguous(**_kwargs: str) -> NoReturn:
        raise ValueError("Expected exactly one item in iterable, but got two")

    monkeypatch.setattr("devinfra.pr_visuals.publisher.find_reviewable_pull_request", ambiguous)
    _forbid(monkeypatch, "download_visual_tests", "write_baseline_pointers")

    with pytest.raises(ValueError, match="Expected exactly one"):
        main()

    assert [(check["conclusion"], check["external_id"]) for check in publisher_run.checks] == [("failure", PR_CHECK_ID)]
    assert publisher_run.comments == [], "with no PR identified there is nowhere to comment"


class PublishedObjects:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.events: list[str] = []

    def head_object(self, **kwargs: str) -> dict[str, int]:
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[key])}

    def put_object(self, **kwargs: Any) -> None:
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["ContentType"] == "image/png"
        assert kwargs["CacheControl"].endswith("immutable")
        key = kwargs["Key"]
        assert key not in self.objects
        self.puts.append(key)
        self.objects[key] = kwargs["Body"]
        self.events.append(key)

    def upload_file(self, path: Path, bucket: str, key: str, **kwargs: dict[str, str]) -> None:
        assert kwargs["ExtraArgs"]["CacheControl"].endswith("immutable")
        self.objects[key] = path.read_bytes()
        self.events.append(key)


def test_upload_reuses_shared_images_and_publishes_indexes_last(tmp_path: Path) -> None:
    source = _single_asset_test(tmp_path / "source")
    client = PublishedObjects()
    for sha in ("a" * 40, "b" * 40):
        bundle = build_bundle([source], tmp_path / "site", commit_sha=sha, repository="r")
        client.events.clear()
        upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key=f"reviews/{sha}", client=client)
        if sha == "a" * 40:
            assert client.events[0].startswith("assets/")
        first_index = next(index for index, key in enumerate(client.events) if key.endswith("index.html"))
        assert all(not key.endswith("index.html") for key in client.events[:first_index])
        assert client.events[-1] == f"reviews/{sha}/index.html"
    assert len(client.puts) == 1
    assert client.objects[client.puts[0]] == (source.directory / "screen.png").read_bytes()
    assert not any(key.startswith("commits/") for key in client.objects)


@pytest.mark.parametrize(
    ("stage", "code"), [("head", "AccessDenied"), ("put", "InternalError"), ("put", "PreconditionFailed")]
)
def test_shared_image_upload_errors_do_not_publish_broken_reviews(tmp_path: Path, stage: str, code: str) -> None:
    source = _single_asset_test(tmp_path / "source")
    bundle = build_bundle([source], tmp_path / "site", commit_sha="a" * 40, repository="r")

    class RacingOrFailingS3(PublishedObjects):
        def head_object(self, **kwargs: str) -> dict[str, int]:
            if stage == "head":
                raise ClientError({"Error": {"Code": code}}, "HeadObject")
            return super().head_object(**kwargs)

        def put_object(self, **kwargs: Any) -> None:
            if code == "PreconditionFailed":
                self.objects[kwargs["Key"]] = kwargs["Body"]
            raise ClientError({"Error": {"Code": code}}, "PutObject")

    client = RacingOrFailingS3()
    if code == "PreconditionFailed":
        upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="reviews/sha", client=client)
        assert client.events[-1] == "reviews/sha/index.html"
    else:
        with pytest.raises(ClientError):
            upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="reviews/sha", client=client)
        assert not client.events


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

    site = tmp_path / "site"
    assert by_path["same.png"].get("baseline_sha256") is None
    assert (site / "assets" / f"{by_path['same.png']['candidate_sha256']}.png").read_bytes() == (
        candidate_dir / "same.png"
    ).read_bytes()
    for path in ("changed.png", "gone.png"):
        key = f"commits/{base_sha}/tests/{slug}/{path}"
        assert (site / "assets" / f"{by_path[path]['baseline_sha256']}.png").read_bytes() == objects[key]
    assert (site / "assets" / f"{by_path['changed.png']['diff_sha256']}.png").is_file()
    assert by_path["gone.png"].get("candidate_sha256") is None
    assert by_path["same.png"].get("diff_sha256") is None
    assert not list(bundle.rglob("*.png"))
    review_tests = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests
    comment = success_comment_body(
        repository="r",
        commit_sha=head_sha,
        url=f"https://s3.test/visuals/reviews/{head_sha}/",
        review_tests=review_tests,
        base_sha=base_sha,
    )
    for path, variant in (
        ("changed.png", "candidate"),
        ("changed.png", "baseline"),
        ("changed.png", "diff"),
        ("gone.png", "baseline"),
    ):
        assert f"https://s3.test/visuals/assets/{by_path[path][f'{variant}_sha256']}.png" in comment
    assert f"tests/{slug}/index.html#changed.png" in comment


def _single_asset_test(directory: Path, label: str = "//ex:visuals") -> DownloadedVisualTest:
    directory.mkdir(parents=True, exist_ok=True)
    _png(directory / "screen.png", (10, 20, 30, 255))
    manifest = VisualReviewManifest.model_validate(
        {"schema": "ducktape.visual-review.v1", "title": "Ex", "assets": [{"path": "screen.png", "label": "screen"}]}
    )
    return DownloadedVisualTest(label, target_slug(label), manifest, directory)


@pytest.mark.parametrize("pointer_fallback", [False, True])
def test_shared_images_are_reused_across_targets_and_new_baselines(tmp_path: Path, pointer_fallback: bool) -> None:
    site = tmp_path / "site"
    first = _single_asset_test(tmp_path / "first", label="//a:visual")
    second = _single_asset_test(tmp_path / "second", label="//b:visual")
    base_sha = "a" * 40
    base = build_bundle([first, second], site, commit_sha=base_sha, repository="r")
    metadata = ReviewBundleMetadata.model_validate_json((base / "metadata.json").read_text())
    digest = metadata.tests[0].assets[0].candidate_sha256
    assert digest == metadata.tests[1].assets[0].candidate_sha256
    assert len(list((site / "assets").glob("*.png"))) == 1
    objects = {path.relative_to(site).as_posix(): path.read_bytes() for path in site.rglob("*") if path.is_file()}
    if pointer_fallback:
        objects[f"baselines/reviews/{first.slug}.json"] = (
            BaselinePointer(commit_sha=base_sha).model_dump_json().encode()
        )
        # An old publisher can keep writing this pointer during the wheel rollout.
        objects[f"baselines/{first.slug}.json"] = BaselinePointer(commit_sha="e" * 40).model_dump_json().encode()
        objects[f"commits/{'e' * 40}/tests/{first.slug}/metadata.json"] = json.dumps(
            {"assets": [{"path": "screen.png", "label": "old"}]}
        ).encode()
        objects[f"commits/{'e' * 40}/tests/{first.slug}/screen.png"] = _png(
            tmp_path / "old.png", (1, 1, 1, 255)
        ).read_bytes()
    # Keep the first target unchanged and change the second so its Before image
    # must come from the shared pool rather than a per-commit PNG.
    _png(second.directory / "screen.png", (20, 30, 40, 255))
    if pointer_fallback:
        objects[f"baselines/reviews/{second.slug}.json"] = (
            BaselinePointer(commit_sha=base_sha).model_dump_json().encode()
        )
    head = build_bundle(
        [first, second],
        site,
        commit_sha="b" * 40,
        repository="r",
        base_sha="c" * 40 if pointer_fallback else base_sha,
        baseline_source=FakeBaselineSource(objects),
    )
    review = ReviewBundleMetadata.model_validate_json((head / "metadata.json").read_text())
    assert review.tests[0].assets[0].classification == "unchanged"
    assert review.tests[0].assets[0].candidate_sha256 == digest
    assert review.tests[0].baseline_fallback == pointer_fallback
    assert review.tests[1].assets[0].classification == "modified"
    assert review.tests[1].assets[0].baseline_sha256 == digest
    assert len(list((site / "assets").glob("*.png"))) == 3


def test_build_bundle_falls_back_to_devel_pointer(tmp_path: Path) -> None:
    test = _single_asset_test(tmp_path / "candidate")
    base_sha = "fedcba9876543210fedcba9876543210fedcba98"
    pointer_sha = "aaaabbbbccccddddeeeeffff0000111122223333"

    # The base commit's bundle lacks this target; only the pointer's commit has it.
    objects: dict[str, bytes] = {
        f"baselines/{test.slug}.json": BaselinePointer(commit_sha=pointer_sha).model_dump_json().encode(),
        f"commits/{pointer_sha}/tests/{test.slug}/metadata.json": json.dumps(
            {"assets": [{"path": "screen.png", "label": "screen"}]}
        ).encode(),
        f"commits/{pointer_sha}/tests/{test.slug}/screen.png": _png(
            tmp_path / "baseline.png", (99, 99, 99, 255)
        ).read_bytes(),
    }

    bundle = build_bundle(
        [test],
        tmp_path / "site",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource(objects),
    )

    metadata = json.loads((bundle / "tests" / test.slug / "metadata.json").read_text())
    assert metadata["base_sha"] == pointer_sha
    assert metadata["baseline_fallback"] is True
    assert metadata["assets"][0]["classification"] == "modified"
    assert metadata["assets"][0]["baseline_stale"] is True
    assert metadata["merge_base_sha"] == base_sha


def test_build_bundle_all_new_when_no_baseline_anywhere(tmp_path: Path) -> None:
    test = _single_asset_test(tmp_path / "candidate")
    base_sha = "fedcba9876543210fedcba9876543210fedcba98"

    bundle = build_bundle(
        [test],
        tmp_path / "site",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        repository="r",
        base_sha=base_sha,
        baseline_source=FakeBaselineSource({}),
    )

    metadata = json.loads((bundle / "tests" / test.slug / "metadata.json").read_text())
    assert metadata["base_sha"] == base_sha
    assert "baseline_fallback" not in metadata
    assert metadata["assets"][0]["classification"] == "new"
    assert metadata["summary"] == {"modified": 0, "new": 1, "removed": 0, "unchanged": 0}
    # No modified asset to prefer — the aggregate index page still gets a thumbnail.
    assert metadata["preview"]["classification"] == "new"


def test_write_baseline_pointers_puts_mutable_json() -> None:
    class FakeS3:
        def __init__(self) -> None:
            self.puts: list[dict[str, object]] = []

        def put_object(self, **kwargs: object) -> None:
            self.puts.append(kwargs)

    client = FakeS3()
    sha = "0123456789abcdef0123456789abcdef01234567"
    write_baseline_pointers(["slug-a", "slug-b"], commit_sha=sha, bucket="visuals", client=client)

    assert [put["Key"] for put in client.puts] == ["baselines/reviews/slug-a.json", "baselines/reviews/slug-b.json"]
    for put in client.puts:
        assert put["Bucket"] == "visuals"
        assert put["CacheControl"] == "no-cache"
        assert put["ContentType"] == "application/json"
        body = put["Body"]
        assert isinstance(body, bytes)
        assert BaselinePointer.model_validate_json(body).commit_sha == sha


def test_diff_check_conclusions() -> None:
    def review_test(summary: ClassificationCounts | None) -> ReviewTest:
        return ReviewTest(target_label="//t:a", slug="s", title="T", assets=[], summary=summary)

    assert diff_check([review_test(None)]) is None

    clean = diff_check([review_test(ClassificationCounts(new=1, unchanged=3))])
    assert clean is not None
    assert clean[0] == "success"

    changed = diff_check([review_test(ClassificationCounts(modified=2))])
    assert changed is not None
    assert changed[0] == "neutral"

    removed_only = diff_check([review_test(ClassificationCounts(removed=1))])
    assert removed_only is not None
    assert removed_only[0] == "neutral"


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
    # Each preview is a before/after/diff table row.
    assert "tests/ex-visuals-abcdef/baseline/a.png" in body
    assert "tests/ex-visuals-abcdef/a.png" in body
    assert "tests/ex-visuals-abcdef/diff/a.png" in body
    assert "tests/ex-visuals-abcdef/diff/b.png" in body
    assert "50.0% changed" in body


def test_success_comment_body_warns_when_comparison_uses_fallback_baseline() -> None:
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=[
            ReviewTest(
                target_label="//ex:visuals",
                slug="ex-visuals-abcdef",
                title="Example UI",
                base_sha="fedcba9876543210fedcba9876543210fedcba98",
                baseline_fallback=True,
                summary=ClassificationCounts(modified=1),
                assets=[],
            )
        ],
        base_sha="f" * 40,
    )

    assert "[!WARNING]" in body
    assert "`fedcba98`" in body


def test_success_comment_body_labels_each_stale_screenshot() -> None:
    def asset(path: str, *, stale: bool | None) -> ReviewAsset:
        return ReviewAsset(path=path, label=path, classification="modified", changed_fraction=0.5, baseline_stale=stale)

    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=[
            ReviewTest(
                target_label="//ex:visuals",
                slug="ex-visuals-abcdef",
                title="Example UI",
                base_sha="fedcba9876543210fedcba9876543210fedcba98",
                merge_base_sha="1111111122222222333333334444444455555555",
                baseline_fallback=True,
                summary=ClassificationCounts(modified=4),
                assets=[
                    asset("a.png", stale=True),
                    asset("b.png", stale=None),
                    asset("c.png", stale=None),
                    asset("d.png", stale=None),
                ],
            )
        ],
        base_sha="1" * 40,
    )

    assert body.count("stale baseline") == 2
    assert (
        "<summary>a.png — 50.0% changed — ⚠️ stale baseline · "
        '<a href="https://v/commits/sha/tests/ex-visuals-abcdef/index.html#a.png">link</a></summary>'
    ) in body.splitlines()
    assert "`fedcba98`, not merge base `11111111`" in body
    assert '<p><strong><a href="https://v/commits/sha/tests/ex-visuals-abcdef/index.html#a.png">a.png</a>' not in body


def test_success_comment_body_hides_zero_count_buckets_per_test() -> None:
    review_tests = [
        ReviewTest(
            target_label="//a:x",
            slug="a",
            title="A",
            summary=ClassificationCounts(modified=4, new=12, removed=0, unchanged=2),
            assets=[],
        ),
        ReviewTest(
            target_label="//b:y",
            slug="b",
            title="B",
            summary=ClassificationCounts(modified=0, new=0, removed=0, unchanged=5),
            assets=[],
        ),
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
    )
    # The target disclosure omits zero-count buckets from its summary.
    lines = body.splitlines()
    assert (
        "<summary><code>//a:x</code> — 4 modified, 12 new · A · "
        '<a href="https://v/commits/sha/tests/a/index.html">link</a></summary>'
    ) in lines
    assert "<summary>1 unchanged target</summary>" in lines
    assert "- [`//b:y`](https://v/commits/sha/tests/b/index.html)" in lines


def test_success_comment_body_is_compact_when_every_affected_test_is_unchanged() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(target_label="//a:x", slug="a", title="A", summary=ClassificationCounts(unchanged=3), assets=[]),
        ReviewTest(target_label="//b:y", slug="b", title="B", summary=ClassificationCounts(unchanged=5), assets=[]),
    ]

    body = success_comment_body(
        repository="r", commit_sha=sha, url="https://v/commits/sha/", review_tests=review_tests, base_sha="f" * 40
    )

    assert body.startswith(COMMENT_MARKER)
    assert f"https://github.com/r/commit/{sha}" in body
    assert "https://v/commits/sha/index.html" in body
    # Compact: no per-target list (the unchanged targets would be named) and no previews.
    assert "//a:x" not in body
    assert "//b:y" not in body
    assert "<details>" not in body
    assert "<img " not in body


def test_success_comment_body_shows_new_previews_when_nothing_modified() -> None:
    """A PR that only adds new fixtures (no existing screenshot changed) must still get image
    previews in the comment, not just the text counts — this was the bug: `_with_diff_previews`
    only ever collected `modified` assets and bailed out with no previews at all otherwise."""
    review_tests = [
        ReviewTest(
            target_label="//ex:visuals",
            slug="ex-visuals-abcdef",
            title="Ex",
            summary=ClassificationCounts(modified=0, new=2, removed=0, unchanged=1),
            assets=[
                ReviewAsset(path="a.png", label="a", classification="new"),
                ReviewAsset(path="b.png", label="b", classification="new"),
                ReviewAsset(path="c.png", label="c", classification="unchanged"),
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
    assert "tests/ex-visuals-abcdef/a.png" in body
    assert "tests/ex-visuals-abcdef/b.png" in body
    assert body.count("<img ") == 2


def test_success_comment_body_groups_changed_tests_and_folds_unchanged_targets() -> None:
    """Changed tests get their own disclosure and the unchanged list stays compact."""
    review_tests = [
        ReviewTest(
            target_label=f"//untouched{index}:visuals",
            slug=f"untouched-{index}",
            title=f"Untouched {index}",
            summary=ClassificationCounts(unchanged=4),
            assets=[ReviewAsset(path="a.png", label="a", classification="unchanged")],
        )
        for index in range(22)
    ] + [
        ReviewTest(
            target_label="//ex:visuals",
            slug="ex-visuals-abcdef",
            title="Ex",
            summary=ClassificationCounts(new=1),
            assets=[ReviewAsset(path="fresh.png", label="fresh", classification="new")],
        )
    ]
    body = success_comment_body(
        repository="r",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://v/commits/sha/",
        review_tests=review_tests,
        base_sha="f" * 40,
        ci_details_url="https://github/actions/runs/123",
    )
    lines = body.splitlines()
    assert "tests/ex-visuals-abcdef/fresh.png" in body
    summary = (
        "<summary><code>//ex:visuals</code> — 1 new · Ex · "
        '<a href="https://v/commits/sha/tests/ex-visuals-abcdef/index.html">link</a> · '
        '<a href="https://github/actions/runs/123">request rerun</a></summary>'
    )
    assert summary in lines
    assert lines.index(summary) < lines.index("<summary>22 unchanged targets</summary>")
    assert "- [`//untouched0:visuals`](https://v/commits/sha/tests/untouched-0/index.html)" in lines
    assert len(body) <= COMMENT_BUDGET


def test_success_comment_body_shows_both_modified_and_new_previews() -> None:
    review_tests = [
        ReviewTest(
            target_label="//ex:visuals",
            slug="s",
            title="Ex",
            summary=ClassificationCounts(modified=1, new=1),
            assets=[
                ReviewAsset(
                    path="changed.png",
                    label="changed",
                    classification="modified",
                    changed_fraction=0.3,
                    changed_pixels=1,
                ),
                ReviewAsset(path="added.png", label="added", classification="new"),
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
    assert "tests/s/diff/changed.png" in body
    assert "tests/s/added.png" in body


def test_success_comment_body_dimension_change_degrades_diff_cell() -> None:
    """No diff overlay exists when dimensions changed — the Diff cell is text."""
    review_tests = [
        ReviewTest(
            target_label="//t:a",
            slug="s",
            title="T",
            summary=ClassificationCounts(modified=1),
            assets=[
                ReviewAsset(
                    path="a.png",
                    label="a",
                    classification="modified",
                    changed_fraction=0.4,
                    changed_pixels=9,
                    candidate_dimensions=[8, 10],
                    baseline_dimensions=[16, 16],
                    dimension_changed=True,
                )
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
    assert "baseline/a.png" in body
    assert "diff/a.png" not in body


def test_success_comment_body_uses_more_of_the_comment_budget_for_previews() -> None:
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
    # Both changed screenshots fit under the expanded budget.
    assert body.count("<img ") == 6
    assert "baseline/a.png" in body
    assert "diff/a.png" in body
    assert "baseline/b.png" in body
    assert "diff/b.png" in body
    assert len(body) <= COMMENT_BUDGET


if __name__ == "__main__":
    pytest_bazel.main()
