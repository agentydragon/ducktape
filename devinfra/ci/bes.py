"""Read a finished Bazel invocation's outputs and test verdicts from BuildBuddy.

`bazel-ci` builds and tests a broad `//...` target set on each devel push. The
publish planners read its invocation streams for output digests and labels instead
of rebuilding targets to decide what changed. An external repository, a `manual`
target, or another configuration can fall outside that sweep and takes the slow
path; devinfra/ci/docs/publish_planning.md lists the known gaps.

BuildBuddy serves the invocation's whole Build Event Protocol stream as JSON. It
contains output files and content digests, per-target test verdicts, and the
labels that produced them.

Two things are easy to get wrong here:

  Build outputs sit behind an indirection test outputs do not have. A target's
  `completed` event names output groups, each group names file-set ids, and a set
  may reference further sets — Bazel shares subsets between targets rather than
  repeating them — so the sets are indexed first and walked transitively.

  A file's name does not identify it. A source file carries an empty path prefix
  while the generated file of the same name sits under `bazel-out/...`, and a
  configuration transition writes to its own prefix. Only prefix + name is unique,
  and where a label is available (`by_label`) it is a better handle still.

Deliberately not via `bbapi`, which grew the same capability for humans: `bbapi`
is itself one of the artifacts in artifact_targets.json, so a release planner
shelling out to the *released* `bbapi` would be circular, and a stale pin would
fail the plan on an unrecognised flag.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping

DEFAULT_BASE_URL = "https://app.buildbuddy.io"

# BuildBuddy serves a big stream; a full `//...` sweep is tens of megabytes.
DEFAULT_TIMEOUT_SECONDS = 120
# A transient BuildBuddy API failure must not make the release/image planners
# immediately fall back to publishing everything. Keep this bounded: the
# planners run in a 30-minute GitHub Actions job and already fail open after
# this reader gives up.
DEFAULT_RETRIES = 3
INITIAL_RETRY_BACKOFF_SECONDS = 1.0


class BuildBuddyError(Exception):
    """The invocation could not be read. Callers decide what an absence means."""


@dataclasses.dataclass(frozen=True)
class Output:
    """One file a target produced, as the build event stream reports it."""

    label: str
    path: str
    #: `bytestream://` location, for the callers that need the bytes and not just
    #: the identity — an image's digest lives *inside* its file.
    uri: str
    digest: str
    size: int
    output_group: str
    #: The aspect that produced this file, or "" for the target's own outputs.
    #: bazel-ci runs the lint aspects, whose report files complete under the same
    #: label; a caller after the target's real artifact must filter these out.
    aspect: str = ""


@dataclasses.dataclass(frozen=True)
class TestSummary:
    """One configuration's terminal summary for a test target."""

    label: str
    configuration_id: str
    status: str
    attempt_count: int | None
    cached_count: int | None


@dataclasses.dataclass(frozen=True)
class Invocation:
    outputs: list[Output]
    #: Target label to Bazel's `overallStatus` (PASSED, FAILED, TIMEOUT, ...).
    test_status: dict[str, str]
    #: Lossless summaries for callers that need to reject ambiguity across configs.
    test_summaries: list[TestSummary] = dataclasses.field(default_factory=list)
    #: Extra provenance from the test invocation; merged output views do not carry it.
    finished: bool = False
    successful: bool = False
    command: str | None = None
    requested_patterns: tuple[str, ...] | None = None
    explicit_cmd_line: tuple[str, ...] | None = None
    #: The full parsed option list, including rc and config expansion.
    cmd_line: tuple[str, ...] | None = None
    commit_sha: str | None = None

    def by_path(self) -> dict[str, Output]:
        """Outputs keyed by their full path. A later duplicate wins, as in Bazel."""
        return {output.path: output for output in self.outputs}

    def by_label(self) -> dict[str, list[Output]]:
        """Outputs grouped by the target that produced them.

        Preferable to deriving a path from a label: an external repository's
        directory name is mangled by bzlmod and cannot be reconstructed, and many
        targets share a basename (most images here are literally named `image`).
        """
        grouped: dict[str, list[Output]] = {}
        for output in self.outputs:
            grouped.setdefault(output.label, []).append(output)
        return grouped


def _api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("BUILDBUDDY_API_KEY", "")
    if not key:
        raise BuildBuddyError("BUILDBUDDY_API_KEY is not set")
    return key


def _retryable(error: BaseException) -> bool:
    """Whether retrying this BuildBuddy read can plausibly succeed."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 425, 429} or error.code >= 500
    return isinstance(error, (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException))


def _get(
    url: str,
    api_key: str,
    timeout: float,
    *,
    retries: int = DEFAULT_RETRIES,
    backoff_seconds: float = INITIAL_RETRY_BACKOFF_SECONDS,
) -> bytes:
    request = urllib.request.Request(url, headers={"x-buildbuddy-api-key": api_key})
    endpoint = url.split("?", maxsplit=1)[0]
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                # urlopen is typed as returning Any, so narrow it here rather than
                # letting an unchecked value reach the parser.
                return bytes(response.read())
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
            if attempt >= retries or not _retryable(e):
                raise BuildBuddyError(f"GET {endpoint} failed: {e}") from e
            delay = backoff_seconds * (2**attempt)
            print(
                f"::warning::BuildBuddy GET {endpoint} failed ({e}); "
                f"retrying in {delay:g}s (attempt {attempt + 2}/{retries + 1})",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise BuildBuddyError(f"GET {endpoint} failed: retry loop exhausted")


def fetch_stream(
    invocation_id: str,
    *,
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    query = urllib.parse.urlencode({"invocation_id": invocation_id, "artifact": "raw_json"})
    return _get(f"{base_url}/file/download?{query}", _api_key(api_key), timeout)


def fetch_blob(
    uri: str, *, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> bytes:
    """Content of a `bytestream://` URI as the event stream reports it."""
    query = urllib.parse.urlencode({"bytestream_url": uri})
    return _get(f"{base_url}/file/download?{query}", _api_key(api_key), timeout)


def _full_path(file: dict) -> str:
    return "/".join([*(file.get("pathPrefix") or []), file["name"]])


def parse(raw: bytes) -> Invocation:
    try:
        events = json.loads(raw)
    except ValueError as e:
        raise BuildBuddyError(f"invocation stream is not JSON: {e}") from e
    if not isinstance(events, list):
        raise BuildBuddyError(f"invocation stream is not a list of events: {type(events).__name__}")

    file_sets = {
        event["id"]["namedSet"]["id"]: event["namedSetOfFiles"] for event in events if "namedSetOfFiles" in event
    }

    def files_in(set_ids: list[dict]) -> list[dict]:
        found: list[dict] = []
        pending = [entry["id"] for entry in set_ids]
        visited: set[str] = set()
        while pending:
            set_id = pending.pop()
            if set_id in visited or set_id not in file_sets:
                continue
            visited.add(set_id)
            group = file_sets[set_id]
            found.extend(group.get("files") or [])
            pending.extend(entry["id"] for entry in group.get("fileSets") or [])
        return found

    outputs: list[Output] = []
    test_status: dict[str, str] = {}
    test_summaries: list[TestSummary] = []
    finished_count = 0
    successful_count = 0
    commands: list[str] = []
    requested_patterns: list[str] = []
    explicit_cmd_lines: list[tuple[str, ...] | None] = []
    cmd_lines: list[tuple[str, ...] | None] = []
    commit_shas: list[str] = []
    for event in events:
        if "testSummary" in event:
            summary = event["testSummary"]
            summary_id = event["id"]["testSummary"]
            label = summary_id["label"]
            configuration = summary_id.get("configuration") or {}
            attempt_count = summary.get("attemptCount")
            cached_count = summary.get("totalNumCached")
            test_status[label] = summary.get("overallStatus", "")
            test_summaries.append(
                TestSummary(
                    label=label,
                    configuration_id=configuration.get("id", ""),
                    status=summary.get("overallStatus", ""),
                    attempt_count=attempt_count
                    if isinstance(attempt_count, int) and not isinstance(attempt_count, bool)
                    else None,
                    cached_count=cached_count
                    if isinstance(cached_count, int) and not isinstance(cached_count, bool)
                    else None,
                )
            )
            continue
        # BuildBuddy's raw_json spells the BuildFinished payload `finished`;
        # the event id remains `buildFinished`.
        if "finished" in event:
            finished_count += 1
            exit_code = event["finished"].get("exitCode") or {}
            if exit_code.get("name") == "SUCCESS":
                successful_count += 1
        if started := event.get("started"):
            command = started.get("command")
            if isinstance(command, str):
                commands.append(command)
        if options := event.get("optionsParsed"):
            explicit = options.get("explicitCmdLine")
            if isinstance(explicit, list) and all(isinstance(arg, str) for arg in explicit):
                explicit_cmd_lines.append(tuple(explicit))
            else:
                explicit_cmd_lines.append(None)
            cmd_line = options.get("cmdLine")
            if isinstance(cmd_line, list) and all(isinstance(arg, str) for arg in cmd_line):
                cmd_lines.append(tuple(cmd_line))
            else:
                cmd_lines.append(None)
        if pattern := event.get("id", {}).get("pattern"):
            requested_patterns.extend(value for value in pattern.get("pattern", []) if isinstance(value, str))
        if workspace_status := event.get("workspaceStatus"):
            commit_shas.extend(
                item.get("value", "")
                for item in workspace_status.get("item", [])
                if item.get("key") == "COMMIT_SHA" and isinstance(item.get("value"), str)
            )
        completed = event.get("completed")
        if completed is None:
            continue
        label = event["id"]["targetCompleted"]["label"]
        aspect = event["id"]["targetCompleted"].get("aspect", "")
        for group in completed.get("outputGroup") or []:
            outputs.extend(
                Output(
                    label=label,
                    path=_full_path(file),
                    uri=file.get("uri", ""),
                    digest=file.get("digest", ""),
                    size=int(file.get("length", 0)),
                    output_group=group.get("name", ""),
                    aspect=aspect,
                )
                for file in files_in(group.get("fileSets") or [])
            )
    # The file-set DAG can list the same output through multiple branches.
    command = commands[0] if len(commands) == 1 else None
    explicit_cmd_line = explicit_cmd_lines[0] if len(explicit_cmd_lines) == 1 else None
    cmd_line = cmd_lines[0] if len(cmd_lines) == 1 else None
    commit_sha_values = set(commit_shas)
    commit_sha = (
        next(iter(commit_sha_values)) if len(commit_sha_values) == 1 and next(iter(commit_sha_values)) else None
    )
    return Invocation(
        outputs=list(dict.fromkeys(outputs)),
        test_status=test_status,
        test_summaries=test_summaries,
        finished=finished_count == 1,
        successful=finished_count == 1 and successful_count == 1,
        command=command,
        requested_patterns=tuple(requested_patterns) if requested_patterns else None,
        explicit_cmd_line=explicit_cmd_line,
        cmd_line=cmd_line,
        commit_sha=commit_sha,
    )


def artifact_output(by_label: Mapping[str, list[Output]], label: str) -> Output | str:
    """The one file `label` published as its artifact, or a reason it has none.

    "Artifact" means the target's own default output group — not lint-aspect
    reports completing under the same label, and not `_validation` outputs. A
    release/pin target publishes exactly one such file; anything else is a reason
    string the caller turns into its own failure mode (the planner fails open,
    the asset resolver fails loud).
    """
    artifacts = sorted(
        {o for o in by_label.get(label, []) if not o.aspect and o.output_group == "default"}, key=lambda o: o.path
    )
    if not artifacts:
        return f"{label}: not reported by the build"
    if len(artifacts) > 1:
        paths = ", ".join(o.path for o in artifacts)
        return f"{label}: reports {len(artifacts)} default outputs ({paths}); an artifact target must publish one"
    return artifacts[0]


def merge(invocations: Iterable[Invocation]) -> Invocation | None:
    """One view of several streams, or None if there are none.

    `bazel-ci` reports two for one commit — `bazel test //...` then
    `bazel build //...` — and each reports the same outputs for every non-test
    target it built, so the result is deduplicated.
    """
    outputs: list[Output] = []
    test_status: dict[str, str] = {}
    empty = True
    for invocation in invocations:
        empty = False
        outputs.extend(invocation.outputs)
        test_status.update(invocation.test_status)
    return None if empty else Invocation(outputs=list(dict.fromkeys(outputs)), test_status=test_status)


def read(invocation_id: str, **kwargs) -> Invocation:
    return parse(fetch_stream(invocation_id, **kwargs))
