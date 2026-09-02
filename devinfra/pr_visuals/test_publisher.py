from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image

from devinfra.ci.invocation_ids import invocation_id
from devinfra.pr_visuals.publisher import (
    COMMENT_BUDGET,
    BaselinePointer,
    ClassificationCounts,
    DownloadedVisualTest,
    ReviewAsset,
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
    write_baseline_pointers,
)
from util.visual_diff import compare_pngs
from util.visual_review import VisualReviewAsset, VisualReviewManifest


def _png(path: Path, color: tuple[int, int, int, int] = (10, 20, 30, 255), size: tuple[int, int] = (8, 8)) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


class FakeBaselineSource:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _search_reply(*invocations: dict[str, object]) -> Callable[..., bytes]:
    return lambda _request: json.dumps({"invocation": list(invocations)}).encode()


def _cas(blobs: dict[str, bytes]) -> Callable[[urllib.request.Request], bytes]:
    """Serve blobs by the `bytestream://` URI the artifact listing advertised.

    An unlisted URI is a KeyError, which is the point: the publisher may only ask for
    a blob the listing named.
    """

    def fetch(request: urllib.request.Request) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        return blobs[query["bytestream_url"][0]]

    return fetch


def _manifest(title: str, *assets: str) -> bytes:
    return json.dumps(
        {
            "schema": "ducktape.visual-review.v1",
            "title": title,
            "assets": [{"path": asset, "label": asset.removesuffix(".png")} for asset in assets],
        }
    ).encode()


def _listing(artifacts: list[dict[str, str]]) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    return fake_run


def test_the_full_sweep_wins_over_an_affected_set_run_at_the_same_commit() -> None:
    """The case that made #4927 insufficient: one commit, several CI runs. Only the
    `//...` sweep carries the visual manifests; an affected-set run at the same commit
    is complete and real and has none. Picking by commit must prefer the sweep."""
    found = find_test_invocations(
        run_id="33066954750",
        run_attempt="1",
        commit_sha="5ee3b732cc68b03ef86a1b6e95598f54d796e8f2",
        api_key="key",
        fetch=_search_reply(
            {"invocationId": "affected-set", "pattern": ["//cluster/k8s:cluster_all_files"]},
            {"invocationId": "full-sweep", "pattern": ["//..."]},
        ),
    )
    assert found == ["full-sweep"]


def test_a_commit_buildbuddy_does_not_know_falls_back_to_the_derived_ids() -> None:
    """A PR run records the merge SHA, so its invocation is not findable by head SHA —
    and a run cancelled before Bazel started has no invocation at all. The by-run
    derivation is the only handle on either, so it stays as the fallback."""
    found = find_test_invocations(
        run_id="33060467222", run_attempt="1", commit_sha="deadbeef", api_key="key", fetch=_search_reply()
    )
    assert found == [
        str(invocation_id(run_id="33060467222", attempt="1", role="test")),
        str(invocation_id(run_id="33060467222", attempt="1", role="build")),
    ]


def test_the_derivation_separates_every_input() -> None:
    """A re-run must not fold into the run it replaces, and build must not claim test's
    ID: BuildBuddy merges two invocations sharing an ID rather than rejecting one."""
    base = invocation_id(run_id="33060467222", attempt="1", role="test")
    assert invocation_id(run_id="33060467223", attempt="1", role="test") != base
    assert invocation_id(run_id="33060467222", attempt="2", role="test") != base
    assert invocation_id(run_id="33060467222", attempt="1", role="build") != base


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
    with pytest.raises(RuntimeError, match="all BuildBuddy artifact queries failed"):
        list_ci_artifacts(["a", "b"], run=lambda *_a, **_k: broken)


def test_download_visual_tests_groups_manifests_by_executed_target(tmp_path: Path) -> None:
    artifacts = [
        {
            "label": "//haku/console/frontend:screenshots",
            "name": "test.outputs/visual-review.json",
            "uri": "bytestream://manifest-haku",
        },
        {
            "label": "//haku/console/frontend:screenshots",
            "name": "test.outputs/preview.png",
            "uri": "bytestream://preview-haku",
        },
        {
            "label": "//aiquota/gnome:test_render",
            "name": "test.outputs/visual-review.json",
            "uri": "bytestream://manifest-aiquota",
        },
        {"label": "//aiquota/gnome:test_render", "name": "test.outputs/hot.png", "uri": "bytestream://hot-aiquota"},
    ]
    commands: list[list[str | Path]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[3] == "missing":
            return subprocess.CompletedProcess(command, 1, "", "invocation not found")
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    tests = download_visual_tests(
        ["missing", "real"],
        tmp_path / "tests",
        api_key="key",
        fetch=_cas(
            {
                "bytestream://manifest-haku": _manifest("Haku Console", "preview.png"),
                "bytestream://preview-haku": b"png",
                "bytestream://manifest-aiquota": _manifest("AI quota", "hot.png"),
                "bytestream://hot-aiquota": b"png",
            }
        ),
        run=fake_run,
    )

    assert [test.target_label for test in tests] == [
        "//aiquota/gnome:test_render",
        "//haku/console/frontend:screenshots",
    ]
    assert (tests[0].directory / "hot.png").read_bytes() == b"png"
    assert commands[1] == [Path("bbapi"), "artifact", "list", "real", "--json"]


def test_bbapi_is_asked_once_per_invocation_however_many_files_a_commit_has(tmp_path: Path) -> None:
    """The artifact listing resolves every blob's URI at once, so the per-file fetch goes
    straight to the CAS. Reintroducing a per-file `bbapi artifact download` would refetch
    the invocation's whole build event stream — 33 MB on a `//...` sweep — for each of a
    commit's ~294 files, which is what made a publish take a quarter of an hour."""
    artifacts = [
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "bytestream://manifest"},
        *(
            {"label": "//ui:screenshots", "name": f"test.outputs/shot-{index}.png", "uri": f"bytestream://shot-{index}"}
            for index in range(12)
        ),
    ]
    shots = [f"shot-{index}.png" for index in range(12)]
    calls: list[list[str | Path]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(artifacts), "")

    tests = download_visual_tests(
        ["invocation"],
        tmp_path / "tests",
        api_key="key",
        fetch=_cas(
            {"bytestream://manifest": _manifest("UI", *shots)}
            | {f"bytestream://shot-{index}": b"png" for index in range(12)}
        ),
        run=fake_run,
    )

    assert len(tests[0].manifest.assets) == 12
    assert calls == [[Path("bbapi"), "artifact", "list", "invocation", "--json"]]


def test_download_visual_tests_rejects_missing_declared_asset(tmp_path: Path) -> None:
    artifacts = [
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "bytestream://manifest"}
    ]

    with pytest.raises(ValueError, match=r"references missing artifact missing\.png"):
        download_visual_tests(
            ["invocation"],
            tmp_path / "tests",
            api_key="key",
            fetch=_cas({"bytestream://manifest": _manifest("UI", "missing.png")}),
            run=_listing(artifacts),
        )


def test_download_visual_tests_deduplicates_equivalent_manifests(tmp_path: Path) -> None:
    artifacts = [
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "bytestream://manifest-first"},
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "bytestream://manifest-retry"},
        {"label": "//ui:screenshots", "name": "test.outputs/screen.png", "uri": "bytestream://screen"},
    ]
    manifest = _manifest("UI", "screen.png")

    tests = download_visual_tests(
        ["invocation"],
        tmp_path / "tests",
        api_key="key",
        fetch=_cas(
            {
                "bytestream://manifest-first": manifest,
                "bytestream://manifest-retry": manifest,
                "bytestream://screen": b"png",
            }
        ),
        run=_listing(artifacts),
    )

    assert len(tests) == 1
    assert (tests[0].directory / "screen.png").read_bytes() == b"png"


def test_download_visual_tests_rejects_conflicting_manifests(tmp_path: Path) -> None:
    artifacts = [
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "manifest-one"},
        {"label": "//ui:screenshots", "name": "test.outputs/visual-review.json", "uri": "manifest-two"},
    ]

    with pytest.raises(ValueError, match="exposed conflicting visual manifests from 2 results"):
        download_visual_tests(
            ["invocation"],
            tmp_path / "tests",
            api_key="key",
            fetch=_cas(
                {"manifest-one": _manifest("UI 1", "screen-1.png"), "manifest-two": _manifest("UI 2", "screen-2.png")}
            ),
            run=_listing(artifacts),
        )


def test_download_visual_tests_treats_null_artifact_list_as_empty(tmp_path: Path) -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "null", "")

    assert download_visual_tests(["invocation"], tmp_path / "tests", api_key="key", run=fake_run) == []


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

    assert bundle == tmp_path / "site" / "commits" / sha
    assert "//example:visuals" in (bundle / "index.html").read_text()
    target_page = bundle / "tests" / target_slug("//example:visuals") / "index.html"
    assert 'id="screen.png"' in target_page.read_text()
    assert (target_page.parent / "screen.png").read_bytes() == b"png"


def test_build_bundle_rejects_abbreviated_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        build_bundle([], tmp_path, commit_sha="0123456", repository="repo")


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
    assert "[Open visual review](https://visuals/commits/sha/index.html)" in success
    assert "tests/example-visuals-" in success
    assert "Visual review failed" in failure
    assert "publisher failed while processing this Bazel CI run" in failure
    assert "invalid or incomplete" not in failure
    assert "missing artifact screen.png" in failure

    warning = success_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        url="https://visuals/commits/sha/",
        review_tests=review_tests,
        ci_conclusion="failure",
        ci_failures=["//haku/console:x_test"],
    )
    assert "visual artifacts that arrived are shown below" in warning
    assert "//haku/console:x_test" in warning

    no_visuals = no_visual_comment_body(
        repository="agentydragon/ducktape",
        commit_sha=sha,
        ci_conclusion="failure",
        ci_failures=["//haku/console:x_test"],
        details_url="https://github/actions/runs/1",
    )
    assert "No visual artifacts were available" in no_visuals
    assert "https://github/actions/runs/1" in no_visuals


@pytest.mark.parametrize(
    ("existing_body", "expected_edit"),
    [
        (
            "<!-- pr-visuals -->\n## Visual review failed for "
            "[`01234567`](https://github.com/agentydragon/ducktape/commit/"
            "0123456789abcdef0123456789abcdef01234567)",
            True,
        ),
        (
            "<!-- pr-visuals -->\n## Visual review for "
            "[`fedcba98`](https://github.com/agentydragon/ducktape/commit/"
            "fedcba9876543210fedcba9876543210fedcba98)",
            True,
        ),
        (
            "<!-- pr-visuals -->\n## Visual review for "
            "[`01234567`](https://github.com/agentydragon/ducktape/commit/"
            "0123456789abcdef0123456789abcdef01234567)\n\n"
            "[Open visual review](https://visuals/commits/01234567/index.html)",
            False,
        ),
        (
            "<!-- pr-visuals -->\n## Visual review for "
            "[`01234567`](https://github.com/agentydragon/ducktape/commit/"
            "0123456789abcdef0123456789abcdef01234567)\n\n"
            "> Bazel CI concluded `failure`.\n\nNo visual artifacts were available from the Bazel CI run.",
            True,
        ),
        (None, False),
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
        repository="agentydragon/ducktape",
        pull_request=4594,
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        body="replacement",
        token="token",
    )

    assert edits == (["replacement"] if expected_edit else [])
    assert created == []


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

    def forbid(name: str) -> Callable[..., None]:
        return lambda **_kwargs: pytest.fail(f"a cancelled run must not reach {name}")

    for forbidden in ("upsert_pull_request_comment", "refresh_stale_pull_request_comment", "write_baseline_pointers"):
        monkeypatch.setattr(f"devinfra.pr_visuals.publisher.{forbidden}", forbid(forbidden))
    downloaded: list[list[str]] = []

    def record(invocations: list[str], _destination: Path, *, api_key: str) -> list[object]:
        downloaded.append(invocations)
        return []

    monkeypatch.setattr("devinfra.pr_visuals.publisher.download_visual_tests", record)
    # BuildBuddy holds nothing under this commit — a PR run records the merge SHA — so
    # the lookup falls through to the IDs derived from the run.
    monkeypatch.setattr("devinfra.pr_visuals.publisher._read", lambda _request: b'{"invocation": []}')
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "key")
    monkeypatch.setattr(
        "sys.argv",
        [
            "publisher",
            "--ci-run-id",
            "33060467222",
            "--ci-run-attempt",
            "1",
            "--work-dir",
            str(tmp_path / "work"),
            "--sha",
            "0123456789abcdef0123456789abcdef01234567",
            "--repository",
            "agentydragon/ducktape",
            "--endpoint",
            "https://s3.example",
            "--bucket",
            "pr-visuals",
            "--public-base-url",
            "https://s3.example/pr-visuals",
            "--ci-conclusion",
            "cancelled",
            "--pull-request",
            "4858",
        ],
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
    assert checks[0]["commit_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert "uperseded" in str(checks[0]["summary"])


def test_upload_publishes_all_indexes_last(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    target = bundle / "tests" / "example"
    target.mkdir(parents=True)
    (target / "screen.png").write_bytes(b"png")
    (target / "index.html").write_text("target")
    (bundle / "metadata.json").write_text("{}")
    (bundle / "index.html").write_text("root")

    class FakeS3:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def upload_file(self, _path: Path, _bucket: str, key: str, **kwargs: dict[str, str]) -> None:
            assert kwargs["ExtraArgs"]["CacheControl"].endswith("immutable")
            self.keys.append(key)

    client = FakeS3()
    upload_bundle(bundle, endpoint="https://s3.test", bucket="visuals", key="commits/sha", client=client)
    first_index = next(index for index, key in enumerate(client.keys) if key.endswith("index.html"))
    assert all(not key.endswith("index.html") for key in client.keys[:first_index])
    assert client.keys[-1] == "commits/sha/index.html"


def test_compare_pngs_classifies_exact_diffs(tmp_path: Path) -> None:
    identical = _png(tmp_path / "identical.png")
    assert compare_pngs(_png(tmp_path / "a.png"), identical).classification == "unchanged"

    modified = compare_pngs(_png(tmp_path / "a.png"), _png(tmp_path / "b.png", (10, 20, 31, 255)))
    assert modified.classification == "modified"
    assert modified.changed_pixels == 64
    assert modified.dimension_changed is False
    assert modified.diff_overlay is not None

    resized = compare_pngs(_png(tmp_path / "a.png"), _png(tmp_path / "c.png", size=(8, 10)))
    assert resized.classification == "modified"
    assert resized.dimension_changed is True


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

    assert (test_dir / "baseline" / "same.png").exists()
    assert (test_dir / "baseline" / "changed.png").exists()
    assert (test_dir / "baseline" / "gone.png").exists()
    assert (test_dir / "diff" / "changed.png").exists()
    assert not (test_dir / "diff" / "same.png").exists()
    for candidate in ("same.png", "changed.png", "added.png"):
        assert (test_dir / candidate).exists()
    assert not (test_dir / "gone.png").exists()


def _single_asset_test(directory: Path, label: str = "//ex:visuals") -> DownloadedVisualTest:
    directory.mkdir(parents=True, exist_ok=True)
    _png(directory / "screen.png", (10, 20, 30, 255))
    manifest = VisualReviewManifest.model_validate(
        {"schema": "ducktape.visual-review.v1", "title": "Ex", "assets": [{"path": "screen.png", "label": "screen"}]}
    )
    return DownloadedVisualTest(label, target_slug(label), manifest, directory)


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

    assert [put["Key"] for put in client.puts] == ["baselines/slug-a.json", "baselines/slug-b.json"]
    for put in client.puts:
        assert put["Bucket"] == "visuals"
        assert put["CacheControl"] == "no-cache"
        assert put["ContentType"] == "application/json"
        body = put["Body"]
        assert isinstance(body, bytes)
        assert BaselinePointer.model_validate_json(body).commit_sha == sha


def test_diff_check_conclusions() -> None:
    def review_test(summary: ClassificationCounts | None, *, fallback: bool = False) -> ReviewTest:
        return ReviewTest(
            target_label="//t:a", slug="s", title="T", assets=[], summary=summary, baseline_fallback=fallback or None
        )

    assert diff_check([review_test(None)]) is None

    clean = diff_check([review_test(ClassificationCounts(new=1, unchanged=3))])
    assert clean is not None
    assert clean[0] == "success"
    assert "1 new" in clean[1]

    changed = diff_check([review_test(ClassificationCounts(modified=2), fallback=True)])
    assert changed is not None
    assert changed[0] == "neutral"
    assert "2 modified" in changed[1]
    assert "devel-latest fallback baseline" in changed[1]

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
    assert "| Before | After | Diff |" in body
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
    assert "exact PR-base visual baseline was unavailable" in body
    assert "`fedcba98`" in body
    assert "not attributable solely to this PR" in body


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
    # Exact-line membership, not substring: the headline totals legitimately spell out
    # "0 removed" too (it always reports all four buckets), so a bare `in body` check would
    # pass even if the per-test bullet still leaked its own zero segments.
    lines = body.splitlines()
    assert "- [`//a:x`](https://v/commits/sha/tests/a/index.html): 4 modified, 12 new" in lines
    assert "- [`//b:y`](https://v/commits/sha/tests/b/index.html): unchanged" in lines


def test_success_comment_body_is_compact_when_every_affected_test_is_unchanged() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    review_tests = [
        ReviewTest(target_label="//a:x", slug="a", title="A", summary=ClassificationCounts(unchanged=3), assets=[]),
        ReviewTest(target_label="//b:y", slug="b", title="B", summary=ClassificationCounts(unchanged=5), assets=[]),
    ]

    body = success_comment_body(
        repository="r", commit_sha=sha, url="https://v/commits/sha/", review_tests=review_tests, base_sha="f" * 40
    )

    assert body == "\n".join(
        [
            "<!-- pr-visuals -->",
            "## Visual review for [`01234567`](https://github.com/r/commit/0123456789abcdef0123456789abcdef01234567)",
            "",
            "No visual changes among the 2 affected Bazel test targets. "
            "[Open visual review](https://v/commits/sha/index.html).",
        ]
    )


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
    assert "### New screenshots" in body
    assert "tests/ex-visuals-abcdef/a.png" in body
    assert "tests/ex-visuals-abcdef/b.png" in body
    assert body.count("<img ") == 2


def test_success_comment_body_folds_unchanged_targets_and_keeps_the_new_preview() -> None:
    """A sweep over many untouched targets must not push the one new screenshot out of the
    comment: the unchanged targets fold under a details block, and the preview stays."""
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
    )
    lines = body.splitlines()
    assert "### New screenshots" in lines
    assert "tests/ex-visuals-abcdef/fresh.png" in body
    assert "<summary>22 unchanged targets</summary>" in lines
    # The changed target is listed in the open, before the fold.
    assert lines.index(
        "- [`//ex:visuals`](https://v/commits/sha/tests/ex-visuals-abcdef/index.html): 1 new"
    ) < lines.index("<details>")
    assert "- [`//untouched0:visuals`](https://v/commits/sha/tests/untouched-0/index.html): unchanged" in lines
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
    assert "### Top changes" in body
    assert "### New screenshots" in body
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
    assert "_(dimensions changed)_" in body
    assert "diff/a.png" not in body


def test_success_comment_body_drops_previews_over_budget() -> None:
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
    # Over budget with two previews → falls back to one (three imgs: before/after/diff).
    assert body.count("<img ") == 3
    assert "baseline/a.png" in body
    assert "diff/a.png" in body
    assert "b.png" not in body
    assert len(body) <= COMMENT_BUDGET


if __name__ == "__main__":
    pytest_bazel.main()
