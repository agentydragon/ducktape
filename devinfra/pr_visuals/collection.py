"""Collect versioned test output records from exact BuildBuddy test results.

The source is Bazel's undeclared test outputs, not a test framework. Records are
downloaded in one batch; payloads retain their advertised CAS URIs so a consumer
can compare digests without downloading bytes.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from devinfra.pr_visuals.artifacts import (
    BESTestSummary,
    Download,
    ListedArtifact,
    Runner,
    download_blobs,
    list_ci_artifacts,
)
from devinfra.testing.output_records import RECORDS_DIR, TEST_OUTPUTS_DIR, OutputRecord, is_canonical_output_path

logger = logging.getLogger(__name__)

_PREFIX = f"test.outputs/{TEST_OUTPUTS_DIR}/"
_RECORDS = f"{_PREFIX}{RECORDS_DIR}/"


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
class SourceResult:
    key: ResultKey
    status: str | None
    summary: BESTestSummary | None
    files: tuple[SourceFile, ...]
    issues: tuple[str, ...] = ()


def _record_name(name: str) -> bool:
    if not name.startswith(_RECORDS):
        return False
    relative = name.removeprefix(_RECORDS)
    return "/" not in relative and is_canonical_output_path(relative) and PurePosixPath(relative).suffix == ".json"


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


def _result_key(listed: ListedArtifact) -> ResultKey:
    source = listed.artifact.id.test_result
    return ResultKey(
        listed.invocation_id,
        source.label,
        source.configuration.id if source.configuration else None,
        source.run,
        source.shard,
        source.attempt,
    )


def collect_outputs(
    invocations: list[str],
    repository: str,
    *,
    targets: Iterable[str] | None = None,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
) -> CollectedOutputs:
    """List test artifacts once per invocation, then collect their output records."""
    logger.info("Listing test artifacts from %d invocation(s)", len(invocations))
    listed = list_ci_artifacts(invocations, bbapi=bbapi, run=run)
    expected = set(targets) if targets is not None else None
    included = (
        expected
        if expected is not None
        else {item.artifact.id.test_result.label for item in listed if item.artifact.file.name.startswith(_RECORDS)}
    )
    by_result: dict[ResultKey, list[ListedArtifact]] = defaultdict(list)
    for item in listed:
        key = _result_key(item)
        if key.target in included:
            by_result[key].append(item)
    records = [item for items in by_result.values() for item in items if _record_name(item.artifact.file.name)]
    logger.info("Listed %d artifacts; downloading %d output records", len(listed), len(records))
    contents: dict[int, bytes] = {}
    download_issue: str | None = None
    if records:
        with tempfile.TemporaryDirectory(prefix="test-output-records-") as directory:
            downloads = [
                Download(item.artifact.file.uri, item.invocation_id, Path(directory) / f"{index}.json")
                for index, item in enumerate(records)
            ]
            try:
                download_blobs(downloads, bbapi=bbapi, run=run)
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or "").strip() or str(error)
                download_issue = f"bbapi record download failed: {detail}"
            # Keep materialized records for diagnostics even when the batch fails.
            contents = {
                id(item): download.destination.read_bytes()
                for item, download in zip(records, downloads, strict=True)
                if download.destination.is_file()
            }
    logger.info("Selecting outputs from %d records", len(contents))
    sources: list[SourceResult] = []
    for key, items in by_result.items():
        statuses = {item.artifact.status for item in items}
        summary = items[0].artifact.test_summary
        source_issues: list[str] = []
        if len(statuses) != 1:
            source_issues.append(f"conflicting statuses {sorted(str(s) for s in statuses)}")
        if any(item.artifact.test_summary != summary for item in items):
            source_issues.append("missing or inconsistent test summary")
            summary = None
        if download_issue is not None and any(item.artifact.file.name.startswith(_RECORDS) for item in items):
            source_issues.append(download_issue)
        files = []
        for item in items:
            digest, size = _cas_digest(item.artifact.file.uri) or (None, None)
            files.append(
                SourceFile(item.artifact.file.name, item.artifact.file.uri, digest, size, contents.get(id(item)))
            )
        sources.append(
            SourceResult(
                key, next(iter(statuses)) if len(statuses) == 1 else None, summary, tuple(files), tuple(source_issues)
            )
        )
    return collect_materialized_outputs(sources, repository, targets=expected)


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
        record: str | None = None,
        related_results: tuple[ResultKey, ...] = (),
    ) -> None:
        nonlocal fatal
        fatal = True
        issue = CollectionIssue(message, result=key, related_results=related_results, output=output, record=record)
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
        by_run: dict[int | None, list[ResultKey]] = defaultdict(list)
        for key in keys:
            by_run[key.run].append(key)

        expected_runs = range(1, run_count + 1)
        if by_run.keys() != set(expected_runs):
            issue(representative, f"missing or unexpected runs (expected 1..{run_count})")
            incomplete_configurations.add(configured)
        expected_shards = set(range(1, shard_count + 1))
        observed_attempt_count = 0
        for run_number in expected_runs:
            run_results = by_run.get(run_number, [])
            if {key.shard for key in run_results} != expected_shards:
                issue(representative, f"run {run_number} has missing or unexpected shards (expected 1..{shard_count})")
                incomplete_configurations.add(configured)
            observed_attempt_count += max((key.attempt or 0 for key in run_results), default=0)
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
