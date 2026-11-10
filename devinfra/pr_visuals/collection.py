"""Collect versioned test output records from exact BuildBuddy test results.

The source is Bazel's undeclared test outputs, not a test framework. Records are
downloaded in one batch; payloads retain their advertised CAS URIs so a consumer
can compare digests without downloading bytes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from devinfra.pr_visuals.artifacts import ListedArtifact, Runner, list_ci_artifacts
from devinfra.pr_visuals.record_cache import RecordCache
from devinfra.testing.output_records import OutputRecord

logger = logging.getLogger(__name__)

_PREFIX = "test.outputs/test-outputs/"
_RECORDS = _PREFIX + "records/"


@dataclass(frozen=True)
class OutputKey:
    repository: str
    target: str
    case_id: str
    checkpoint: str | None
    role: str


@dataclass(frozen=True)
class ResultKey:
    invocation: str
    target: str
    configuration: str | None
    run: int | None
    shard: int | None
    attempt: int | None


@dataclass(frozen=True)
class CollectionIssue:
    message: str
    result: ResultKey | None = None
    related_results: tuple[ResultKey, ...] = ()
    output: OutputKey | None = None
    artifact: str | None = None
    record: str | None = None
    invocation: str | None = None
    target: str | None = None
    configuration: str | None = None


@dataclass(frozen=True)
class OutputObservation:
    key: OutputKey
    result: ResultKey
    status: str | None
    uri: str
    digest: str | None
    size: int | None
    record_uri: str
    path: str
    label: str | None
    title: str | None


@dataclass(frozen=True)
class ResultCoverage:
    result: ResultKey
    status: str | None
    overall_status: str | None
    record_count: int
    selected: bool
    issues: tuple[CollectionIssue, ...]
    retried_by: int | None = None


@dataclass(frozen=True)
class CollectedOutputs:
    observations: tuple[OutputObservation, ...]
    selected: tuple[OutputObservation, ...]
    results: tuple[ResultCoverage, ...]
    issues: tuple[CollectionIssue, ...]
    complete: bool


@dataclass(frozen=True)
class SourceFile:
    """One advertised output; record contents are present when materialized."""

    name: str
    uri: str
    digest: str | None
    size: int | None
    contents: bytes | None = None


@dataclass(frozen=True)
class SourceSummary:
    overall_status: str | None
    run_count: int | None
    shard_count: int | None
    attempt_count: int | None
    total_run_count: int | None


@dataclass(frozen=True)
class SourceResult:
    key: ResultKey
    status: str | None
    summary: SourceSummary | None
    files: tuple[SourceFile, ...]
    issues: tuple[str, ...] = ()


def _relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError(f"invalid output path {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"output path is not canonical and relative: {value!r}")
    return path


def _record_name(name: str) -> bool:
    if not name.startswith(_RECORDS):
        return False
    try:
        relative = _relative_path(name.removeprefix(_RECORDS))
    except ValueError:
        return False
    return len(relative.parts) == 1 and relative.suffix == ".json"


def _trusted_digest(file: SourceFile) -> bool:
    return (
        isinstance(file.digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", file.digest) is not None
        and type(file.size) is int
        and file.size >= 0
    )


def _cas_digest(uri: str) -> tuple[str, int] | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "bytestream" or not parsed.netloc or parsed.query or parsed.fragment:
        return None
    match = re.search(r"(?:^|/)blobs/([0-9a-fA-F]{64})/(0|[1-9][0-9]*)$", parsed.path)
    if match is None:
        return None
    return match.group(1).lower(), int(match.group(2))


def _validate_scope(repository: str, target: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
        part in (".", "..") for part in repository.split("/")
    ):
        raise ValueError(f"invalid canonical repository {repository!r}")
    if not target.startswith("//") or target.count(":") != 1 or any(char in target for char in "\\\x00\n\r"):
        raise ValueError(f"invalid canonical target {target!r}")
    package, name = target[2:].split(":")
    if not name or any(part in ("", ".", "..") for part in package.split("/") if package):
        raise ValueError(f"invalid canonical target {target!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"invalid canonical target {target!r}")


def _result_key(listed: ListedArtifact) -> ResultKey | None:
    source = listed.artifact.id.test_result
    if source is None:
        return None
    return ResultKey(
        listed.invocation_id,
        source.label,
        source.configuration.id if source.configuration else None,
        source.run,
        source.shard,
        source.attempt,
    )


def _download_records(
    downloads: list[tuple[ListedArtifact, Path]], *, destination: Path, bbapi: Path, run: Runner
) -> str | None:
    if not downloads:
        return None
    destination.mkdir(parents=True, exist_ok=True)
    manifest = [
        {"uri": listed.artifact.file.uri, "destination": str(path), "invocation_id": listed.invocation_id}
        for listed, path in downloads
    ]
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination, delete=False) as stream:
        json.dump(manifest, stream)
        manifest_path = Path(stream.name)
    try:
        result = run(
            [bbapi, "--timeout", "30s", "blob", "download", "--manifest", str(manifest_path)],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            return f"bbapi record download failed: {result.stderr.strip()}"
    finally:
        manifest_path.unlink(missing_ok=True)
    return None


def collect_outputs(
    invocations: list[str],
    repository: str,
    destination: Path,
    *,
    targets: Iterable[str] | None = None,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
    record_cache: RecordCache | None = None,
) -> CollectedOutputs:
    """List test artifacts once per invocation, then collect their output records."""
    logger.info("Listing test artifacts from %d invocation(s)", len(invocations))
    listed = list_ci_artifacts(invocations, bbapi=bbapi, run=run, require_all=True)
    return collect_listed_outputs(
        listed, repository, destination, targets=targets, bbapi=bbapi, run=run, record_cache=record_cache
    )


def collect_listed_outputs(
    listed: list[ListedArtifact],
    repository: str,
    destination: Path,
    *,
    targets: Iterable[str] | None = None,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
    record_cache: RecordCache | None = None,
) -> CollectedOutputs:
    """Adapt a native BES listing into materialized, result-scoped records."""
    record_labels = {
        item.artifact.label
        for item in listed
        if item.artifact.id.test_result is not None and item.artifact.file.name.startswith(_RECORDS)
    }
    included = set(targets) if targets is not None else record_labels
    by_result: dict[ResultKey, list[ListedArtifact]] = defaultdict(list)
    for item in listed:
        if item.artifact.id.test_result is not None and item.artifact.label in included:
            key = _result_key(item)
            if key is not None:
                by_result[key].append(item)
    downloads: list[tuple[ListedArtifact, Path]] = []
    destination.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="records-", dir=destination))
    for key, items in by_result.items():
        for index, item in enumerate(items):
            if _record_name(item.artifact.file.name):
                staged = staging_root / hashlib.sha256(repr(key).encode()).hexdigest() / f"{index}.json"
                staged.parent.mkdir(parents=True, exist_ok=True)
                downloads.append((item, staged))
    if record_cache is not None and downloads:
        record_cache.load()
    contents: dict[int, bytes] = {}
    misses: list[tuple[ListedArtifact, Path]] = []
    # A CAS body can be listed under several result identities. Materialize it once;
    # each result still receives its own fresh artifact, status and summary validation.
    by_digest: dict[tuple[str, int], Path] = {}
    for item, path in downloads:
        cas = _cas_digest(item.artifact.file.uri)
        cached = record_cache.get(*cas) if record_cache is not None and cas is not None else None
        if cached is not None:
            contents[id(item)] = cached
        elif cas is not None and cas in by_digest:
            pass
        else:
            misses.append((item, path))
            if cas is not None:
                by_digest[cas] = path
    logger.info(
        "Listed %d artifacts; %d output records cached, downloading %d distinct records",
        len(listed),
        len(contents),
        len(misses),
    )
    download_error = _download_records(misses, destination=destination, bbapi=bbapi, run=run)
    for item, path in downloads:
        if id(item) in contents:
            continue
        cas = _cas_digest(item.artifact.file.uri)
        source = by_digest.get(cas, path) if cas is not None else path
        if source.is_file():
            contents[id(item)] = source.read_bytes()
    logger.info("Selecting outputs from %d records", len(downloads))
    sources: list[SourceResult] = []
    for key, items in by_result.items():
        statuses = {item.artifact.status for item in items}
        summaries = {repr(item.artifact.test_summary) for item in items}
        source_issues: list[str] = []
        if len(statuses) != 1:
            source_issues.append(f"conflicting statuses {sorted(str(s) for s in statuses)}")
        if len(summaries) != 1:
            source_issues.append("missing or inconsistent test summary")
        if download_error is not None and any(item.artifact.file.name.startswith(_RECORDS) for item in items):
            source_issues.append(download_error)
        summary = items[0].artifact.test_summary
        files = []
        for item in items:
            digest, size = _cas_digest(item.artifact.file.uri) or (None, None)
            files.append(
                SourceFile(item.artifact.file.name, item.artifact.file.uri, digest, size, contents.get(id(item)))
            )
        sources.append(
            SourceResult(
                key,
                next(iter(statuses)) if len(statuses) == 1 else None,
                SourceSummary(
                    summary.overall_status,
                    summary.run_count,
                    summary.shard_count,
                    summary.attempt_count,
                    summary.total_run_count,
                )
                if summary is not None and len(summaries) == 1
                else None,
                tuple(files),
                tuple(source_issues),
            )
        )
    collected = collect_materialized_outputs(sources, repository, targets=targets)
    if record_cache is not None and record_cache.writable and collected.complete:
        for item, _ in downloads:
            cas = _cas_digest(item.artifact.file.uri)
            body = contents.get(id(item))
            if cas is not None and body is not None:
                record_cache.remember(*cas, body)
        record_cache.save()
    return collected


def local_output_result(root: Path, *, result: ResultKey, status: str | None, summary: SourceSummary) -> SourceResult:
    """Read one completed undeclared-output tree without Bazel or pytest imports.

    The caller supplies execution provenance and status explicitly. File URIs point
    to local materialized payloads, while digest and size come from their bytes.
    """
    if root.is_symlink():
        raise ValueError("output root must not be a symlink")
    output_root = root / "test-outputs"
    if output_root.is_symlink() or not output_root.is_dir():
        raise ValueError("test-outputs/ must be a real directory")
    files: list[SourceFile] = []
    for part in ("records", "files"):
        folder = output_root / part
        if folder.is_symlink():
            raise ValueError(f"{part}/ must not be a symlink")
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise ValueError(f"{part}/ must be a directory")
        for file in sorted(folder.rglob("*")):
            if file.is_symlink() or any(parent.is_symlink() for parent in file.parents if parent != output_root):
                raise ValueError(f"symlink in test output path: {file}")
            if not file.is_file():
                continue
            relative = file.relative_to(output_root).as_posix()
            _relative_path(relative)
            data = file.read_bytes()
            files.append(
                SourceFile(
                    _PREFIX + relative,
                    file.resolve().as_uri(),
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    data if part == "records" else None,
                )
            )
    return SourceResult(result, status, summary, tuple(files))


def collect_materialized_outputs(
    results: Iterable[SourceResult], repository: str, *, targets: Iterable[str] | None = None
) -> CollectedOutputs:
    """Validate and select outputs from any result-scoped materialized source.

    `targets` can include an expected target with no listed results. Without it,
    scope is targets exposing a record. The caller must provide honest result
    status and summary counts; absence of an entire source result is unknowable.
    """
    _validate_scope(repository, "//:scope")
    source_results = list(results)
    record_labels = {
        source.key.target for source in source_results if any(file.name.startswith(_RECORDS) for file in source.files)
    }
    included = set(targets) if targets is not None else record_labels
    for target in included:
        _validate_scope(repository, target)
    by_result: dict[ResultKey, SourceResult] = {}
    duplicates: list[ResultKey] = []
    for source in source_results:
        if source.key.target in included:
            if source.key in by_result:
                duplicates.append(source.key)
            else:
                by_result[source.key] = source

    errors: list[CollectionIssue] = []
    per_result: dict[ResultKey, list[CollectionIssue]] = defaultdict(list)
    retried_by: dict[ResultKey, int] = {}
    fatal = False

    if targets is not None:
        missing_targets = included - {key.target for key in by_result}
        errors.extend(CollectionIssue("no listed test results", target=target) for target in sorted(missing_targets))
        fatal = bool(missing_targets)

    def issue(
        key: ResultKey,
        message: str,
        *,
        output: OutputKey | None = None,
        artifact: str | None = None,
        record: str | None = None,
        related_results: tuple[ResultKey, ...] = (),
    ) -> None:
        nonlocal fatal
        fatal = True
        issue = CollectionIssue(
            message, result=key, related_results=related_results, output=output, artifact=artifact, record=record
        )
        errors.append(issue)
        per_result[key].append(issue)

    for key in duplicates:
        issue(key, "duplicate result identity")
    for key, source in by_result.items():
        for message in source.issues:
            issue(key, message)

    observations: list[OutputObservation] = []
    for key, source in by_result.items():
        advertised: dict[str, SourceFile] = {}
        records: list[SourceFile] = []
        record_names: set[str] = set()
        for file in source.files:
            previous = advertised.setdefault(file.name, file)
            if previous.uri != file.uri or previous.digest != file.digest or previous.size != file.size:
                issue(key, f"artifact {file.name!r} has conflicting references")
            if file.name.startswith(_RECORDS):
                if not _record_name(file.name):
                    issue(key, f"invalid record artifact name {file.name!r}")
                elif file.name in record_names:
                    issue(key, f"duplicate record artifact {file.name!r}")
                else:
                    record_names.add(file.name)
                    records.append(file)
        claimed: set[OutputKey] = set()
        for record_file in records:
            if record_file.contents is None:
                issue(key, f"record was not materialized: {record_file.name!r}")
                continue
            if record_file.digest is not None or record_file.size is not None:
                if not _trusted_digest(record_file):
                    issue(key, f"record {record_file.name!r} has an invalid content digest or size")
                    continue
                if (
                    hashlib.sha256(record_file.contents).hexdigest() != record_file.digest
                    or len(record_file.contents) != record_file.size
                ):
                    issue(key, f"record {record_file.name!r} does not match its advertised digest or size")
                    continue
            try:
                record = OutputRecord.model_validate_json(record_file.contents)
                output_key = OutputKey(repository, key.target, record.case_id, record.checkpoint, record.role)
                if output_key in claimed:
                    issue(key, "duplicate output identity", output=output_key, record=record_file.name)
                    continue
                claimed.add(output_key)
                payload_name = _PREFIX + record.path
                payload = advertised.get(payload_name)
                if payload is None:
                    raise ValueError(f"declared payload {payload_name!r} is absent from this exact result")
                if not _trusted_digest(payload):
                    issue(key, f"payload {payload_name!r} has no trusted content digest and size")
                observations.append(
                    OutputObservation(
                        output_key,
                        key,
                        source.status,
                        payload.uri,
                        payload.digest,
                        payload.size,
                        record_file.uri,
                        record.path,
                        record.label,
                        record.title,
                    )
                )
            except (OSError, ValueError) as exc:
                issue(key, f"invalid record {record_file.name!r}: {exc}")

    selected_results: set[ResultKey] = set()
    by_configuration: dict[tuple[str, str, str], list[ResultKey]] = defaultdict(list)
    by_shard: dict[tuple[str, str, str, int, int], list[ResultKey]] = defaultdict(list)
    for key in by_result:
        if key.configuration is None or key.run is None or key.shard is None or key.attempt is None:
            issue(key, "incomplete native test result identity")
            continue
        by_configuration[key.invocation, key.target, key.configuration].append(key)
        by_shard[key.invocation, key.target, key.configuration, key.run, key.shard].append(key)
    incomplete_configurations: set[tuple[str, str, str]] = set()
    for configured, keys in by_configuration.items():
        representative = keys[0]
        summaries = {by_result[key].summary for key in keys}
        if len(summaries) != 1 or None in summaries:
            issue(representative, "missing or inconsistent test summary")
            incomplete_configurations.add(configured)
            continue
        summary = next(iter(summaries))
        assert summary is not None
        overall = summary.overall_status
        run_count = summary.run_count
        shard_count = summary.shard_count
        attempt_count = summary.attempt_count
        total_run_count = summary.total_run_count
        # Proto JSON omits shardCount for some unsharded results. An observed
        # shard > 1 without a count is ambiguous, so it cannot be selected.
        if shard_count is None and {key.shard for key in keys} == {1}:
            shard_count = 1
        if (
            overall not in ("PASSED", "FLAKY", "FAILED")
            or run_count is None
            or shard_count is None
            or attempt_count is None
            or total_run_count is None
            or run_count < 1
            or shard_count < 1
            or attempt_count < 1
        ):
            issue(representative, "test summary does not confirm complete successful runs")
            incomplete_configurations.add(configured)
            continue
        if overall == "FAILED":
            invocation, target, configuration = configured
            errors.append(
                CollectionIssue(
                    "configured test summary failed; successful shards remain available for diagnosis",
                    invocation=invocation,
                    target=target,
                    configuration=configuration,
                )
            )
            fatal = True
        if total_run_count != len(keys):
            issue(representative, f"listed {len(keys)} result attempts but summary reports {total_run_count}")
            incomplete_configurations.add(configured)
        if {key.run for key in keys} != set(range(1, run_count + 1)):
            issue(representative, f"missing or unexpected runs (expected 1..{run_count})")
            incomplete_configurations.add(configured)
        for run_number in range(1, run_count + 1):
            shards = {key.shard for key in keys if key.run == run_number}
            if shards != set(range(1, shard_count + 1)):
                issue(representative, f"run {run_number} has missing or unexpected shards (expected 1..{shard_count})")
                incomplete_configurations.add(configured)
        observed_attempt_count = sum(
            max(((key.attempt or 0) for key in keys if key.run == run_number), default=0)
            for run_number in range(1, run_count + 1)
        )
        if observed_attempt_count != attempt_count:
            issue(
                representative,
                f"listed runs have attempt count {observed_attempt_count} but summary reports {attempt_count}",
            )
            incomplete_configurations.add(configured)
    for attempts in by_shard.values():
        terminal = max(attempts, key=lambda value: value.attempt or 0)
        expected_attempts = set(range(1, (terminal.attempt or 0) + 1))
        if {key.attempt for key in attempts} != expected_attempts:
            issue(terminal, "attempt sequence has missing or unexpected results")
        for earlier in attempts:
            if earlier != terminal:
                assert terminal.attempt is not None
                errors.append(CollectionIssue(f"retried by attempt {terminal.attempt}", result=earlier))
                retried_by[earlier] = terminal.attempt
        status = by_result[terminal].status
        assert terminal.configuration is not None
        configured = (terminal.invocation, terminal.target, terminal.configuration)
        if status == "PASSED" and not per_result[terminal] and configured not in incomplete_configurations:
            selected_results.add(terminal)
        else:
            issue(terminal, f"terminal attempt has no confirmed successful result (status={status})")

    selected = [observation for observation in observations if observation.result in selected_results]
    claims: dict[tuple[str, str, str, int, OutputKey], ResultKey] = {}
    for observation in selected:
        result_key = observation.result
        assert result_key.configuration is not None
        assert result_key.run is not None
        claim = (result_key.invocation, result_key.target, result_key.configuration, result_key.run, observation.key)
        previous_claim = claims.setdefault(claim, result_key)
        if previous_claim != result_key:
            issue(
                result_key,
                "output identity also claimed across shards",
                output=observation.key,
                related_results=(previous_claim,),
            )

    coverage: list[ResultCoverage] = []
    for key, source in sorted(by_result.items(), key=lambda pair: repr(pair[0])):
        coverage.append(
            ResultCoverage(
                key,
                source.status,
                source.summary.overall_status if source.summary is not None else None,
                sum(_record_name(file.name) for file in source.files),
                key in selected_results,
                tuple(per_result[key]),
                retried_by.get(key),
            )
        )
    return CollectedOutputs(tuple(observations), tuple(selected), tuple(coverage), tuple(errors), not fatal)
