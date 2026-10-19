"""Read-only retention planning for PR visual-review objects."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError
from github import Auth, Github

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
ASSET_KEY = re.compile(r"^assets/([0-9a-f]{64})\.png$")
BUNDLE_KEY = re.compile(r"^(commits|reviews)/([0-9a-f]{40})/(.+)$")
POINTER_KEY = re.compile(r"^baselines/(?:reviews/)?([^/]+)\.json$")
CLOSED_PR_GRACE = timedelta(hours=8)
DEFAULT_RETENTION = timedelta(days=3)
ACTIVE_RUN_STATUSES = ("queued", "in_progress", "requested", "waiting", "pending")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime


@dataclass(frozen=True)
class Bundle:
    prefix: str
    sha: str
    objects: tuple[StoredObject, ...]

    @property
    def newest(self) -> datetime:
        return max(obj.last_modified for obj in self.objects)

    @property
    def size(self) -> int:
        return sum(obj.size for obj in self.objects)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return value.astimezone(UTC)


def _validate_sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or FULL_SHA.fullmatch(value) is None:
        raise ValueError(f"{context} is not a full lowercase commit SHA: {value!r}")
    return value


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    return value


def _metadata_document(body: bytes, key: str) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"malformed retained metadata at {key}: {error}") from error
    return _object(value, key)


def _metadata_refs(value: dict[str, Any], context: str) -> tuple[set[str], set[str]]:
    """Return dependency SHAs and referenced image digests."""
    dependencies: set[str] = set()
    digests: set[str] = set()

    def read_test(test_value: Any, test_context: str) -> None:
        test = _object(test_value, test_context)
        slug = test.get("slug")
        if not isinstance(slug, str) or not slug:
            raise ValueError(f"{test_context}.slug is missing or invalid")
        for field in ("base_sha", "merge_base_sha"):
            sha = test.get(field)
            if sha is not None:
                dependencies.add(_validate_sha(sha, f"{test_context}.{field}"))
        assets = test.get("assets")
        if not isinstance(assets, list):
            raise ValueError(f"{test_context}.assets must be a list")
        for index, asset_value in enumerate(assets):
            asset = _object(asset_value, f"{test_context}.assets[{index}]")
            for field in ("candidate_sha256", "baseline_sha256", "diff_sha256"):
                digest = asset.get(field)
                if digest is not None:
                    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                        raise ValueError(f"{test_context}.assets[{index}].{field} is invalid")
                    digests.add(digest)

    if "tests" in value:
        commit_sha = value.get("commit_sha")
        if commit_sha is not None:
            _validate_sha(commit_sha, f"{context}.commit_sha")
        tests = value["tests"]
        if not isinstance(tests, list):
            raise ValueError(f"{context}.tests must be a list")
        for index, test in enumerate(tests):
            read_test(test, f"{context}.tests[{index}]")
    else:
        read_test(value, context)
    return dependencies, digests


def collect_github_roots(repository: Any, observed_at: datetime) -> dict[str, set[str]]:
    """Read current PR and Actions roots from a PyGithub repository object."""
    roots: dict[str, set[str]] = defaultdict(set)
    for pull in repository.get_pulls(state="open"):
        head_sha = _validate_sha(pull.head.sha, f"open PR #{pull.number} head")
        roots[head_sha].add(f"open_pr_head:{pull.number}")
        merge_base_sha = _validate_sha(
            repository.compare(pull.base.sha, head_sha).merge_base_commit.sha, f"open PR #{pull.number} merge base"
        )
        roots[merge_base_sha].add(f"open_pr_merge_base:{pull.number}")

    closed_after = _utc(observed_at) - CLOSED_PR_GRACE
    for pull in repository.get_pulls(state="closed", sort="updated", direction="desc"):
        # Closed PRs are ordered by update time; a PR closed before this cutoff
        # cannot be followed by one with an older update time.
        updated_at = pull.updated_at
        if updated_at is not None and _utc(updated_at) < closed_after:
            break
        closed_at = pull.closed_at
        if closed_at is not None and _utc(closed_at) >= closed_after:
            head_sha = _validate_sha(pull.head.sha, f"recently closed PR #{pull.number} head")
            roots[head_sha].add(f"recently_closed_pr_head:{pull.number}")

    for status in ACTIVE_RUN_STATUSES:
        for run in repository.get_workflow_runs(status=status):
            head_sha = _validate_sha(run.head_sha, f"active Actions run {run.id} head")
            roots[head_sha].add(f"active_actions_head:{run.id}")
            for pull in run.pull_requests or []:
                pr_sha = _validate_sha(pull.head.sha, f"active Actions run {run.id} PR head")
                roots[pr_sha].add(f"active_actions_pr:{run.id}")
    return dict(roots)


def _list_objects(client: Any, bucket: str) -> list[StoredObject]:
    objects: list[StoredObject] = []
    # SeaweedFS accepts 10,000-key pages; avoid hundreds of small round trips.
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, PaginationConfig={"PageSize": 10000}):
        objects.extend(
            StoredObject(key=item["Key"], size=item["Size"], last_modified=_utc(item["LastModified"]))
            for item in page.get("Contents", [])
        )
        LOGGER.info("Listed %d objects", len(objects))
    return objects


def _read_s3_object(client: Any, bucket: str, key: str) -> bytes | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return cast(bytes, response["Body"].read())


def build_retention_report(
    objects: Iterable[StoredObject],
    *,
    fetch_object: Callable[[str], bytes | None],
    github_roots: dict[str, set[str]],
    observed_at: datetime,
    cutoff: datetime,
) -> dict[str, Any]:
    """Build a fail-closed, non-executable plan from one observed object listing."""
    observed_at, cutoff = _utc(observed_at), _utc(cutoff)
    if cutoff > observed_at:
        raise ValueError("cutoff must not be later than observed_at")
    by_key: dict[str, StoredObject] = {}
    bundles_by_prefix: dict[str, list[StoredObject]] = defaultdict(list)
    bundles_by_sha: dict[str, list[Bundle]] = defaultdict(list)
    target_dirs_by_prefix: dict[str, set[str]] = defaultdict(set)
    target_metadata_by_prefix: dict[str, list[str]] = defaultdict(list)
    pointers: list[StoredObject] = []
    assets: list[tuple[StoredObject, str]] = []
    unknown_keys: list[str] = []
    blockers: list[str] = []

    for stored in objects:
        if stored.key in by_key:
            blockers.append(f"duplicate listed object key: {stored.key}")
        by_key[stored.key] = stored
        if match := BUNDLE_KEY.fullmatch(stored.key):
            root, sha, _ = match.groups()
            prefix = f"{root}/{sha}/"
            bundles_by_prefix[prefix].append(stored)
            target_dir_match = re.match(r"tests/([^/]+)/", match.group(3))
            if target_dir_match is not None:
                target_dirs_by_prefix[prefix].add(target_dir_match.group(1))
            if re.fullmatch(r"tests/[^/]+/metadata\.json", match.group(3)):
                target_metadata_by_prefix[prefix].append(stored.key)
        elif stored.key.startswith(("commits/", "reviews/")):
            unknown_keys.append(stored.key)
            blockers.append(f"unrecognized bundle key: {stored.key}")
        elif stored.key.startswith("assets/"):
            if match := ASSET_KEY.fullmatch(stored.key):
                assets.append((stored, match.group(1)))
            else:
                unknown_keys.append(stored.key)
        elif stored.key.startswith("baselines/"):
            if POINTER_KEY.fullmatch(stored.key):
                pointers.append(stored)
            else:
                unknown_keys.append(stored.key)
                blockers.append(f"unrecognized baseline pointer key: {stored.key}")
        else:
            unknown_keys.append(stored.key)

    for prefix, members in bundles_by_prefix.items():
        sha = prefix.split("/")[1]
        bundles_by_sha[sha].append(Bundle(prefix, sha, tuple(sorted(members, key=lambda item: item.key))))

    reasons: dict[str, set[str]] = defaultdict(set)
    pointer_values: list[dict[str, str]] = []
    pointer_targets: dict[str, set[str]] = defaultdict(set)
    for pointer in pointers:
        try:
            body = fetch_object(pointer.key)
            if body is None:
                raise ValueError(f"listed pointer disappeared: {pointer.key}")
            value = _metadata_document(body, pointer.key)
            sha = _validate_sha(value.get("commit_sha"), f"{pointer.key}.commit_sha")
            pointer_match = POINTER_KEY.fullmatch(pointer.key)
            assert pointer_match is not None
            slug = pointer_match.group(1)
            reasons[sha].add(f"baseline_pointer:{pointer.key}")
            pointer_targets[sha].add(slug)
            pointer_values.append({"key": pointer.key, "commit_sha": sha})
        except ValueError as error:
            blockers.append(str(error))

    for sha, sha_reasons in github_roots.items():
        _validate_sha(sha, "GitHub protected root")
        reasons[sha].update(sha_reasons)
    existing_shas = set(bundles_by_sha)
    unpublished_github_roots = sorted(
        sha
        for sha in reasons
        if sha not in existing_shas
        and any(reason.startswith(("open_pr_", "recently_closed_pr_", "active_actions_")) for reason in reasons[sha])
    )
    blockers.extend(
        f"baseline pointer references missing bundle SHA {sha}"
        for sha, sha_reasons in reasons.items()
        if sha not in existing_shas and any(reason.startswith("baseline_pointer:") for reason in sha_reasons)
    )

    retained_shas = {
        sha for sha, bundles in bundles_by_sha.items() if any(bundle.newest >= cutoff for bundle in bundles)
    }
    retained_shas.update(sha for sha in reasons if sha in existing_shas)
    referenced_digests: set[str] = set()
    queue = deque(sorted(retained_shas))
    parsed_prefixes: set[str] = set()
    target_metadata_slugs_by_sha: dict[str, set[str]] = defaultdict(set)
    while queue and not blockers:
        sha = queue.popleft()
        for bundle in bundles_by_sha.get(sha, []):
            if bundle.prefix in parsed_prefixes:
                continue
            parsed_prefixes.add(bundle.prefix)
            if len(parsed_prefixes) % 50 == 0:
                LOGGER.info("Reading metadata for retained bundle %d", len(parsed_prefixes))
            aggregate_key = f"{bundle.prefix}metadata.json"
            test_keys = sorted(target_metadata_by_prefix.get(bundle.prefix, []))
            metadata_records: list[tuple[dict[str, Any], str]] = []
            actual_target_slugs: set[str] = set()
            metadata_keys = ([aggregate_key] if aggregate_key in by_key else []) + test_keys
            # The production callback uses a thread-safe boto3 client; callbacks
            # supplied by callers must support concurrent read requests too.
            with ThreadPoolExecutor(max_workers=8) as pool:
                bodies = dict(zip(metadata_keys, pool.map(fetch_object, metadata_keys), strict=True))
            if aggregate_key in by_key:
                body = bodies[aggregate_key]
                if body is None:
                    blockers.append(f"listed retained metadata disappeared: {aggregate_key}")
                    break
                try:
                    metadata = _metadata_document(body, aggregate_key)
                    metadata_sha = metadata.get("commit_sha")
                    if metadata_sha is not None and _validate_sha(metadata_sha, f"{aggregate_key}.commit_sha") != sha:
                        raise ValueError(f"{aggregate_key}.commit_sha does not match bundle SHA {sha}")
                    metadata_records.append((metadata, aggregate_key))
                except ValueError as error:
                    blockers.append(str(error))
                    break
            elif not test_keys:
                blockers.append(f"retained bundle has no metadata: {bundle.prefix}")
                break
            else:
                missing_target_metadata = target_dirs_by_prefix.get(bundle.prefix, set()) - {
                    key.removeprefix(f"{bundle.prefix}tests/").removesuffix("/metadata.json") for key in test_keys
                }
                if missing_target_metadata:
                    blockers.append(
                        f"retained bundle has target files without metadata at {bundle.prefix}: "
                        f"{sorted(missing_target_metadata)}"
                    )
                    break
            for key in test_keys:
                body = bodies[key]
                if body is None:
                    blockers.append(f"listed retained metadata disappeared: {key}")
                    break
                try:
                    metadata = _metadata_document(body, key)
                    target_slug = key.removeprefix(f"{bundle.prefix}tests/").removesuffix("/metadata.json")
                    if metadata.get("slug") != target_slug:
                        raise ValueError(f"{key}.slug does not match its key path")
                    metadata_records.append((metadata, key))
                    actual_target_slugs.add(target_slug)
                except ValueError as error:
                    blockers.append(str(error))
                    break
            if blockers:
                break
            for metadata, key in metadata_records:
                try:
                    dependencies, digests = _metadata_refs(metadata, key)
                except ValueError as error:
                    blockers.append(str(error))
                    break
                referenced_digests.update(digests)
                for dependency in dependencies:
                    reasons[dependency].add(f"metadata_dependency:{bundle.prefix}")
                    if dependency in existing_shas and dependency not in retained_shas:
                        retained_shas.add(dependency)
                        queue.append(dependency)
            if blockers:
                break
            target_metadata_slugs_by_sha[sha].update(actual_target_slugs)

    for sha, target_slugs in pointer_targets.items():
        # Pointer resolution falls back across the old and new bundle prefixes,
        # but requires the actual per-target metadata object either way.
        missing_slugs = target_slugs - target_metadata_slugs_by_sha.get(sha, set())
        if missing_slugs:
            blockers.append(f"baseline pointer for {sha} has no retained target metadata: {sorted(missing_slugs)}")

    missing_referenced_assets = sorted(digest for digest in referenced_digests if f"assets/{digest}.png" not in by_key)
    blockers.extend(
        f"retained metadata references missing shared image assets/{digest}.png" for digest in missing_referenced_assets
    )
    bundles = [bundle for group in bundles_by_sha.values() for bundle in group]
    bundle_candidates = [bundle for bundle in bundles if bundle.sha not in retained_shas and bundle.newest < cutoff]
    bundle_retained = [bundle for bundle in bundles if bundle not in bundle_candidates]
    asset_candidates = [
        (stored, digest)
        for stored, digest in assets
        if stored.last_modified < cutoff and digest not in referenced_digests
    ]
    asset_candidate_keys = {stored.key for stored, _ in asset_candidates}
    bundle_candidate_keys = {obj.key for bundle in bundle_candidates for obj in bundle.objects}

    if blockers:
        # Unknown references can reach any historical bundle or shared image.
        bundle_candidates = []
        bundle_candidate_keys = set()
        asset_candidates = []
        asset_candidate_keys = set()
        bundle_retained = bundles

    candidate_keys = bundle_candidate_keys | asset_candidate_keys
    retained_keys = set(by_key) - candidate_keys
    bundle_candidate_rows = [
        {
            "prefix": bundle.prefix,
            "sha": bundle.sha,
            "newest_object_at": _iso(bundle.newest),
            "object_count": len(bundle.objects),
            "bytes": bundle.size,
            "keys": [obj.key for obj in bundle.objects],
        }
        for bundle in sorted(bundle_candidates, key=lambda item: item.prefix)
    ]
    bundle_retained_rows = [
        {
            "prefix": bundle.prefix,
            "sha": bundle.sha,
            "newest_object_at": _iso(bundle.newest),
            "object_count": len(bundle.objects),
            "bytes": bundle.size,
            "reasons": sorted(
                reasons.get(bundle.sha, set()) or ({"age_grace"} if bundle.newest >= cutoff else {"blocked"})
            ),
        }
        for bundle in sorted(bundle_retained, key=lambda item: item.prefix)
    ]
    asset_candidate_rows = [
        {"key": stored.key, "sha256": digest, "size": stored.size, "last_modified": _iso(stored.last_modified)}
        for stored, digest in sorted(asset_candidates, key=lambda item: item[0].key)
    ]
    candidate_bundle_bytes = sum(bundle.size for bundle in bundle_candidates)
    retained_bundle_bytes = sum(bundle.size for bundle in bundle_retained)
    candidate_asset_bytes = sum(stored.size for stored, _ in asset_candidates)
    retained_asset_bytes = sum(stored.size for stored, _ in assets if stored.key not in asset_candidate_keys)
    return {
        "plan_status": "blocked" if blockers else "candidate_plan",
        "plan_only": True,
        "deletion_performed": False,
        "safe_to_delete_now": False,
        "shared_asset_sweep_safety": "unsafe_without_publisher_coordination",
        "observed_at": _iso(observed_at),
        "cutoff": _iso(cutoff),
        "retention_days": (observed_at - cutoff).total_seconds() / 86400,
        "object_count": len(by_key),
        "object_bytes": sum(obj.size for obj in by_key.values()),
        "candidate_object_count": len(candidate_keys),
        "candidate_bytes": sum(by_key[key].size for key in candidate_keys),
        "retained_object_count": len(retained_keys),
        "retained_bytes": sum(by_key[key].size for key in retained_keys),
        "bundles": {
            "candidate_count": len(bundle_candidates),
            "candidate_bytes": candidate_bundle_bytes,
            "candidate_object_count": len(bundle_candidate_keys),
            "retained_count": len(bundle_retained),
            "retained_bytes": retained_bundle_bytes,
            "candidates": bundle_candidate_rows,
            "retained": bundle_retained_rows,
        },
        "shared_assets": {
            "candidate_count": len(asset_candidates),
            "candidate_bytes": candidate_asset_bytes,
            "retained_count": len(assets) - len(asset_candidates),
            "retained_bytes": retained_asset_bytes,
            "candidates": asset_candidate_rows,
            "safe_to_delete_now": False,
        },
        "preserved_baseline_pointers": sorted(pointer_values, key=lambda item: item["key"]),
        "preserved_baseline_pointer_keys": sorted(pointer.key for pointer in pointers),
        "unpublished_github_roots": unpublished_github_roots,
        "unpublished_metadata_dependencies": sorted(
            sha
            for sha, sha_reasons in reasons.items()
            if sha not in existing_shas and any(reason.startswith("metadata_dependency:") for reason in sha_reasons)
        ),
        "retained_references": {sha: sorted(value) for sha, value in sorted(reasons.items())},
        "retained_unknown_keys": sorted(unknown_keys),
        "blockers": sorted(set(blockers)),
    }


def _parse_timestamp(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="S3-compatible endpoint URL")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--observed-at", help="fixed ISO-8601 observation time; defaults to now")
    parser.add_argument("--cutoff", help="fixed ISO-8601 cutoff; defaults to observed-at minus 3 days")
    args = parser.parse_args()
    observed_at = _parse_timestamp(args.observed_at, datetime.now(UTC))
    cutoff = _parse_timestamp(args.cutoff, observed_at - DEFAULT_RETENTION)
    token = os.environ["GITHUB_TOKEN"]
    s3 = boto3.client("s3", endpoint_url=args.endpoint)
    with Github(auth=Auth.Token(token)) as github:
        repository = github.get_repo(args.repository)
        LOGGER.info("Reading GitHub PR and Actions references")
        roots = collect_github_roots(repository, observed_at)
        LOGGER.info("Collected %d protected commit references; listing bucket", len(roots))
        report = build_retention_report(
            _list_objects(s3, args.bucket),
            fetch_object=lambda key: _read_s3_object(s3, args.bucket, key),
            github_roots=roots,
            observed_at=observed_at,
            cutoff=cutoff,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
