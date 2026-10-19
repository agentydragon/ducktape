from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_bazel

from devinfra.pr_visuals.retention import StoredObject, build_retention_report, collect_github_roots

OBSERVED = datetime(2026, 10, 19, 12, tzinfo=UTC)
CUTOFF = OBSERVED - timedelta(days=3)
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
DIGEST_CANDIDATE = "1" * 64
DIGEST_BASELINE = "2" * 64
DIGEST_DIFF = "3" * 64


class ObjectStore:
    def __init__(self, objects: list[StoredObject], bodies: dict[str, bytes]):
        self.objects = objects
        self.bodies = bodies

    def fetch(self, key: str) -> bytes | None:
        return self.bodies.get(key)


def _asset(digests: tuple[str | None, str | None, str | None] = (None, None, None)) -> dict[str, Any]:
    candidate, baseline, diff = digests
    return {
        "path": "screen.png",
        "label": "Screen",
        "candidate_sha256": candidate,
        "baseline_sha256": baseline,
        "diff_sha256": diff,
    }


def _metadata(
    sha: str,
    *,
    slug: str = "example",
    base_sha: str | None = None,
    merge_base_sha: str | None = None,
    digests: tuple[str | None, str | None, str | None] = (None, None, None),
) -> dict[str, Any]:
    return {
        "repository": "owner/repo",
        "commit_sha": sha,
        "tests": [
            {
                "slug": slug,
                "target_label": "//example:test",
                "title": "Example",
                "base_sha": base_sha,
                "merge_base_sha": merge_base_sha,
                "assets": [_asset(digests)],
            }
        ],
    }


def _add_bundle(
    objects: list[StoredObject],
    bodies: dict[str, bytes],
    root: str,
    sha: str,
    modified: datetime,
    **metadata_fields: Any,
) -> None:
    prefix = f"{root}/{sha}/"
    metadata = _metadata(sha, **metadata_fields)
    slug = metadata["tests"][0]["slug"]
    root_key = f"{prefix}metadata.json"
    target_key = f"{prefix}tests/{slug}/metadata.json"
    objects.extend(StoredObject(key, 10, modified) for key in (root_key, target_key, f"{prefix}index.html"))
    bodies[root_key] = json.dumps(metadata).encode()
    bodies[target_key] = json.dumps(metadata["tests"][0]).encode()


def _report(store: ObjectStore, roots: dict[str, set[str]] | None = None) -> dict[str, Any]:
    return build_retention_report(
        store.objects, fetch_object=store.fetch, github_roots=roots or {}, observed_at=OBSERVED, cutoff=CUTOFF
    )


def test_preserves_both_baseline_pointer_generations_and_reports_exact_old_bundle_keys() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    old = OBSERVED - timedelta(days=10)
    _add_bundle(objects, bodies, "commits", SHA_A, old, slug="legacy")
    _add_bundle(objects, bodies, "reviews", SHA_B, old, slug="current")
    _add_bundle(objects, bodies, "reviews", SHA_C, old)
    for key, sha in (("baselines/legacy.json", SHA_A), ("baselines/reviews/current.json", SHA_B)):
        objects.append(StoredObject(key, 5, old))
        bodies[key] = json.dumps({"commit_sha": sha}).encode()

    report = _report(ObjectStore(objects, bodies))

    assert report["plan_status"] == "candidate_plan"
    assert report["preserved_baseline_pointers"] == [
        {"key": "baselines/legacy.json", "commit_sha": SHA_A},
        {"key": "baselines/reviews/current.json", "commit_sha": SHA_B},
    ]
    assert [row["prefix"] for row in report["bundles"]["candidates"]] == [f"reviews/{SHA_C}/"]
    assert report["bundles"]["candidates"][0]["keys"] == [
        f"reviews/{SHA_C}/index.html",
        f"reviews/{SHA_C}/metadata.json",
        f"reviews/{SHA_C}/tests/example/metadata.json",
    ]


def test_cutoff_boundary_uses_newest_object_and_retains_exact_boundary() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    _add_bundle(objects, bodies, "reviews", SHA_A, CUTOFF)
    _add_bundle(objects, bodies, "commits", SHA_B, CUTOFF - timedelta(microseconds=1))

    report = _report(ObjectStore(objects, bodies))

    assert [row["prefix"] for row in report["bundles"]["retained"]] == [f"reviews/{SHA_A}/"]
    assert [row["prefix"] for row in report["bundles"]["candidates"]] == [f"commits/{SHA_B}/"]


def test_open_pr_roots_walk_transitive_bundle_dependencies_and_keep_all_image_roles() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    old = OBSERVED - timedelta(days=12)
    recent = OBSERVED - timedelta(days=1)
    _add_bundle(
        objects,
        bodies,
        "reviews",
        SHA_A,
        recent,
        base_sha=SHA_B,
        merge_base_sha=SHA_C,
        digests=(DIGEST_CANDIDATE, DIGEST_BASELINE, DIGEST_DIFF),
    )
    _add_bundle(objects, bodies, "commits", SHA_B, old, base_sha=SHA_D)
    _add_bundle(objects, bodies, "reviews", SHA_C, old)
    _add_bundle(objects, bodies, "commits", SHA_D, old)
    _add_bundle(objects, bodies, "reviews", "e" * 40, old)
    objects.extend(
        StoredObject(f"assets/{digest}.png", 20, old)
        for digest in (DIGEST_CANDIDATE, DIGEST_BASELINE, DIGEST_DIFF, "4" * 64)
    )
    objects.append(StoredObject(f"assets/{'5' * 64}.png", 20, recent))

    report = _report(ObjectStore(objects, bodies), {SHA_A: {"open_pr_head:17"}})

    assert {row["prefix"] for row in report["bundles"]["retained"]} == {
        f"reviews/{SHA_A}/",
        f"commits/{SHA_B}/",
        f"reviews/{SHA_C}/",
        f"commits/{SHA_D}/",
    }
    assert [row["prefix"] for row in report["bundles"]["candidates"]] == [f"reviews/{'e' * 40}/"]
    assert {row["key"] for row in report["shared_assets"]["candidates"]} == {f"assets/{'4' * 64}.png"}
    assert {row["key"] for row in report["shared_assets"]["candidates"]}.isdisjoint(
        {f"assets/{digest}.png" for digest in (DIGEST_CANDIDATE, DIGEST_BASELINE, DIGEST_DIFF)}
    )


def test_github_roots_use_actual_merge_base_recent_closures_and_active_action_pr_heads() -> None:
    open_pull = SimpleNamespace(
        number=17, head=SimpleNamespace(sha=SHA_A), base=SimpleNamespace(sha=SHA_B), closed_at=None
    )
    recent_closed = SimpleNamespace(
        number=18,
        head=SimpleNamespace(sha=SHA_C),
        base=SimpleNamespace(sha=SHA_D),
        closed_at=OBSERVED - timedelta(hours=2),
        updated_at=OBSERVED - timedelta(hours=2),
    )
    stale_closed = SimpleNamespace(
        number=19,
        head=SimpleNamespace(sha="e" * 40),
        base=SimpleNamespace(sha=SHA_D),
        closed_at=OBSERVED - timedelta(hours=9),
        updated_at=OBSERVED - timedelta(hours=9),
    )
    run = SimpleNamespace(id=77, head_sha=SHA_D, pull_requests=[SimpleNamespace(head=SimpleNamespace(sha="f" * 40))])

    class Repository:
        def get_pulls(self, *, state: str, **_kwargs: Any) -> list[Any]:
            return [open_pull] if state == "open" else [recent_closed, stale_closed]

        def compare(self, base: str, head: str) -> Any:
            assert (base, head) == (SHA_B, SHA_A)
            return SimpleNamespace(merge_base_commit=SimpleNamespace(sha=SHA_C))

        def get_workflow_runs(self, *, status: str) -> list[Any]:
            return [run] if status == "in_progress" else []

    roots = collect_github_roots(Repository(), OBSERVED)

    assert roots == {
        SHA_A: {"open_pr_head:17"},
        SHA_C: {"open_pr_merge_base:17", "recently_closed_pr_head:18"},
        SHA_D: {"active_actions_head:77"},
        "f" * 40: {"active_actions_pr:77"},
    }


@pytest.mark.parametrize("metadata_mode", ["missing", "malformed_aggregate", "malformed_target"])
def test_retained_metadata_failures_block_all_candidates(metadata_mode: str) -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    recent = OBSERVED - timedelta(hours=2)
    _add_bundle(objects, bodies, "reviews", SHA_A, recent)
    _add_bundle(objects, bodies, "reviews", SHA_B, OBSERVED - timedelta(days=14))
    if metadata_mode == "missing":
        bodies.pop(f"reviews/{SHA_A}/metadata.json")
        bodies.pop(f"reviews/{SHA_A}/tests/example/metadata.json")
        objects = [
            obj
            for obj in objects
            if not obj.key.startswith(f"reviews/{SHA_A}/metadata.json")
            and not obj.key.startswith(f"reviews/{SHA_A}/tests/example/metadata.json")
        ]
    elif metadata_mode == "malformed_aggregate":
        bodies[f"reviews/{SHA_A}/metadata.json"] = b"{"
    else:
        bodies[f"reviews/{SHA_A}/tests/example/metadata.json"] = b"{}"
    objects.append(StoredObject(f"assets/{'9' * 64}.png", 20, OBSERVED - timedelta(days=20)))

    report = _report(ObjectStore(objects, bodies))

    assert report["plan_status"] == "blocked"
    assert report["bundles"]["candidate_count"] == 0
    assert report["shared_assets"]["candidate_count"] == 0
    assert report["candidate_object_count"] == 0
    assert report["blockers"]


def test_unknown_root_keys_are_retained_and_reported_without_guessing() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    _add_bundle(objects, bodies, "reviews", SHA_A, OBSERVED - timedelta(days=9))
    objects.append(StoredObject("root-note.txt", 7, OBSERVED - timedelta(days=90)))
    objects.append(StoredObject(f"assets/{'x' * 64}.png", 8, OBSERVED - timedelta(days=90)))

    report = _report(ObjectStore(objects, bodies))

    assert report["plan_status"] == "candidate_plan"
    assert report["retained_unknown_keys"] == ["assets/" + "x" * 64 + ".png", "root-note.txt"]
    assert "root-note.txt" not in {key for bundle in report["bundles"]["candidates"] for key in bundle["keys"]}
    assert report["retained_object_count"] >= 2


def test_unrecognized_bundle_roots_and_missing_pointer_targets_fail_closed() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    _add_bundle(objects, bodies, "reviews", SHA_A, OBSERVED - timedelta(days=9))
    objects.append(StoredObject("commits/not-a-sha/index.html", 3, OBSERVED - timedelta(days=90)))
    missing_pointer = "baselines/reviews/missing.json"
    objects.append(StoredObject(missing_pointer, 1, OBSERVED - timedelta(days=90)))
    bodies[missing_pointer] = json.dumps({"commit_sha": SHA_D}).encode()

    report = _report(ObjectStore(objects, bodies))

    assert report["plan_status"] == "blocked"
    assert report["bundles"]["candidate_count"] == 0
    assert report["shared_assets"]["candidate_count"] == 0
    assert any("unrecognized bundle key" in blocker for blocker in report["blockers"])
    assert any("missing bundle SHA" in blocker for blocker in report["blockers"])


def test_pointer_needs_actual_target_metadata_even_when_aggregate_lists_the_slug() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    old = OBSERVED - timedelta(days=14)
    _add_bundle(objects, bodies, "reviews", SHA_A, old, slug="pointed-target")
    target_key = f"reviews/{SHA_A}/tests/pointed-target/metadata.json"
    objects = [obj for obj in objects if obj.key != target_key]
    bodies.pop(target_key)
    pointer_key = "baselines/reviews/pointed-target.json"
    objects.append(StoredObject(pointer_key, 1, old))
    bodies[pointer_key] = json.dumps({"commit_sha": SHA_A}).encode()
    _add_bundle(objects, bodies, "commits", SHA_B, old)

    report = _report(ObjectStore(objects, bodies))

    assert report["plan_status"] == "blocked"
    assert report["bundles"]["candidate_count"] == 0
    assert any("no retained target metadata" in blocker for blocker in report["blockers"])


def test_old_asset_candidates_are_explicitly_unsafe_during_concurrent_publisher_reuse() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    digest = "8" * 64
    objects.append(StoredObject(f"assets/{digest}.png", 100, OBSERVED - timedelta(days=20)))

    report = _report(ObjectStore(objects, bodies))

    assert report["shared_assets"]["candidate_count"] == 1
    assert report["shared_assets"]["safe_to_delete_now"] is False
    assert report["shared_asset_sweep_safety"] == "unsafe_without_publisher_coordination"
    assert report["safe_to_delete_now"] is False
    assert report["deletion_performed"] is False


def test_absent_provenance_only_merge_base_does_not_block_a_plan() -> None:
    objects: list[StoredObject] = []
    bodies: dict[str, bytes] = {}
    _add_bundle(objects, bodies, "reviews", SHA_A, OBSERVED - timedelta(hours=1), base_sha=SHA_B, merge_base_sha=SHA_C)
    _add_bundle(objects, bodies, "reviews", SHA_D, OBSERVED - timedelta(days=12))

    report = _report(ObjectStore(objects, bodies), {SHA_A: {"open_pr_head:17"}})

    assert report["plan_status"] == "candidate_plan"
    assert report["unpublished_metadata_dependencies"] == [SHA_B, SHA_C]
    assert [row["prefix"] for row in report["bundles"]["candidates"]] == [f"reviews/{SHA_D}/"]


if __name__ == "__main__":
    pytest_bazel.main()
