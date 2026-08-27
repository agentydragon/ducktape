"""Read a finished Bazel invocation's outputs and test verdicts from BuildBuddy.

`bazel-ci` already builds and tests everything on a devel push — measured on the
`//...` sweep, that covers 41 of 42 image `.digest` outputs and 47 of 50 release
artifacts. The publish planners used to rebuild those in their own `bb remote`
invocations; this reads what the first build already produced instead. What a
`//...` sweep cannot cover — an external repository, a `manual` target, another
configuration — takes the slow path, and devinfra/ci/docs/publish_planning.md
lists which rows those are.

BuildBuddy serves the invocation's whole Build Event Protocol stream as JSON, and
it carries three things at once: every output file with its content digest, the
per-target test verdicts, and the labels that produced them. One request, ~34 MB
and ~2s for a full `//...` sweep.

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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable

DEFAULT_BASE_URL = "https://app.buildbuddy.io"

# BuildBuddy serves a big stream; a full `//...` sweep is tens of megabytes.
DEFAULT_TIMEOUT_SECONDS = 120


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


@dataclasses.dataclass(frozen=True)
class Invocation:
    outputs: list[Output]
    #: Target label to Bazel's `overallStatus` (PASSED, FAILED, TIMEOUT, ...).
    test_status: dict[str, str]

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


def _get(url: str, api_key: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"x-buildbuddy-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # urlopen is typed as returning Any, so narrow it here rather than
            # letting an unchecked value reach the parser.
            return bytes(response.read())
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
        raise BuildBuddyError(f"GET {url.split('?', maxsplit=1)[0]} failed: {e}") from e


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


def _dedup(outputs: Iterable[Output]) -> list[Output]:
    """The same output, listed twice, is one output. Order is preserved.

    Duplicates arise two ways and neither means anything: one target can reach a
    file through two branches of the file-set DAG (proto virtual imports do this),
    and `bazel-ci`'s test and build invocations both report every non-test target
    they built. `by_path` collapsed them for free by being a dict; `by_label`
    groups into lists and would otherwise hand a caller one file twice.
    """
    seen: set[Output] = set()
    unique = []
    for output in outputs:
        if output not in seen:
            seen.add(output)
            unique.append(output)
    return unique


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
    for event in events:
        if summary := event.get("testSummary"):
            test_status[event["id"]["testSummary"]["label"]] = summary.get("overallStatus", "")
            continue
        completed = event.get("completed")
        if completed is None:
            continue
        label = event["id"]["targetCompleted"]["label"]
        for group in completed.get("outputGroup") or []:
            outputs.extend(
                Output(
                    label=label,
                    path=_full_path(file),
                    uri=file.get("uri", ""),
                    digest=file.get("digest", ""),
                    size=int(file.get("length", 0)),
                    output_group=group.get("name", ""),
                )
                for file in files_in(group.get("fileSets") or [])
            )
    return Invocation(outputs=_dedup(outputs), test_status=test_status)


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
    return None if empty else Invocation(outputs=_dedup(outputs), test_status=test_status)


def read(invocation_id: str, **kwargs) -> Invocation:
    return parse(fetch_stream(invocation_id, **kwargs))
