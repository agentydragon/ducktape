"""Read a Bazel invocation's outputs from BuildBuddy.

The `bbapi invocation export` command owns Build Event Protocol parsing. This
module adapts its stable JSON contract to the dataclasses used by publish
planners.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Mapping

from devinfra.buildbuddy.cli import CLIError, run

DEFAULT_BASE_URL = "https://app.buildbuddy.io"
DEFAULT_TIMEOUT_SECONDS = 120


class BuildBuddyError(Exception):
    """The invocation could not be read. Callers decide what an absence means."""


@dataclasses.dataclass(frozen=True)
class Output:
    """One file a target produced, as the build event stream reports it."""

    label: str
    path: str
    uri: str
    digest: str
    size: int
    output_group: str
    aspect: str = ""


@dataclasses.dataclass(frozen=True)
class Invocation:
    outputs: list[Output]
    invocation_id: str | None = None

    def by_path(self) -> dict[str, Output]:
        """Outputs keyed by their full path. A later duplicate wins, as in Bazel."""
        return {output.path: output for output in self.outputs}

    def by_label(self) -> dict[str, list[Output]]:
        """Outputs grouped by the target that produced them.

        Preferable to deriving a path from a label: an external repository's
        directory name is mangled by bzlmod and cannot be reconstructed, and many
        targets share a basename (most images are literally named `image`).
        """
        grouped: dict[str, list[Output]] = {}
        for output in self.outputs:
            grouped.setdefault(output.label, []).append(output)
        return grouped


def fetch_blob(
    uri: str,
    *,
    invocation_id: str | None = None,
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Content of a `bytestream://` URI as the event stream reports it."""
    args = ["blob", "cat", uri]
    if invocation_id is not None:
        args.extend(("--invocation", invocation_id))
    try:
        return run(*args, timeout=timeout, api_key=api_key, base_url=base_url)
    except CLIError as e:
        raise BuildBuddyError(str(e)) from e


def read(
    invocation_id: str,
    *,
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Invocation:
    """Read an invocation through bbapi's stable structured export."""
    try:
        raw = run("invocation", "export", invocation_id, timeout=timeout, api_key=api_key, base_url=base_url)
    except CLIError as e:
        raise BuildBuddyError(str(e)) from e
    try:
        invocation = _invocation_from_export(json.loads(raw))
        if invocation.invocation_id is not None and invocation.invocation_id != invocation_id:
            raise ValueError(f"requested invocation {invocation_id}, received {invocation.invocation_id}")
        return invocation
    except (TypeError, ValueError, KeyError) as e:
        raise BuildBuddyError(f"bbapi invocation export returned invalid JSON: {e}") from e


def _invocation_from_export(document: object) -> Invocation:
    if not isinstance(document, dict):
        raise ValueError("export is not an object")
    outputs = []
    for output in document["outputs"]:
        file = output["file"]
        target = output["id"]["targetCompleted"]
        outputs.append(
            Output(
                label=target["label"],
                path="/".join([*file.get("pathPrefix", []), file["name"]]),
                uri=file.get("uri", ""),
                digest=file.get("digest", ""),
                size=int(file.get("length", 0)),
                output_group=output["outputGroup"],
                aspect=target.get("aspect", ""),
            )
        )
    return Invocation(outputs=outputs, invocation_id=document.get("invocationId"))


def artifact_output(by_label: Mapping[str, list[Output]], label: str) -> Output | str:
    """The one file `label` publishes, or a reason it has none.

    "Artifact" means the target's own default output group, not lint reports
    completing under the same label or `_validation` outputs.
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
    """Combine invocation outputs, deduplicating shared files."""
    outputs: list[Output] = []
    empty = True
    for invocation in invocations:
        empty = False
        outputs.extend(invocation.outputs)
    return None if empty else Invocation(outputs=list(dict.fromkeys(outputs)))
