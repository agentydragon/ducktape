"""Conservative reuse of test summaries from the trusted push-CI invocation.

Only literal test labels with their own terminal summary can use this fast path.
Suite and target-pattern selectors stay in their publication jobs, where Bazel
expands and runs the requested coverage.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path

from devinfra.ci.bes import Invocation, TestSummary
from devinfra.ci.invocation_ids import invocation_id

_LABEL = re.compile(r"//[^:\s]+:[^:\s]+\Z")
_RBE_PIN_ERRORS = (OSError, ValueError, KeyError, TypeError)


def effective_rbe_image(override: str, pins_path: Path = Path("devinfra/image_pins.json")) -> str | None:
    """Resolve the image the publication test step will use, or fail closed."""
    if override:
        return override
    try:
        pins = json.loads(pins_path.read_text())
        image = pins["rbe_container"]["image"]
        digest = pins["rbe_container"]["digest"]
    except _RBE_PIN_ERRORS:
        return None
    if not isinstance(image, str) or not isinstance(digest, str) or not image or not digest:
        return None
    return f"{image}@{digest}"


def _explicit_ci_options(invocation_id_value: str, rbe_image: str) -> tuple[Counter[str], ...]:
    """Accepted explicit option forms from bazel-ci's successful push test."""
    base = Counter(
        [
            "--keep_going",
            "--config=ci",
            "--build_metadata=ROLE=CI",
            "--build_metadata=DISABLE_TARGET_TRACKING=false",
            f"--remote_default_exec_properties=container-image=docker://{rbe_image}",
        ]
    )
    # Older bazel-ci versions passed --config=rbe explicitly. Current CI gets
    # it from the workspace .bazelrc, so both command forms are valid evidence.
    bases = (base, base + Counter(["--config=rbe"]))
    invocation_id_arg = Counter([f"--invocation_id={invocation_id_value}"])
    pattern_arg = Counter(["//..."])
    return tuple(
        accepted_base + extra
        for accepted_base in bases
        for extra in (Counter(), invocation_id_arg, pattern_arg, invocation_id_arg + pattern_arg)
    )


def _normalized_explicit_args(args: tuple[str, ...]) -> Counter[str] | None:
    values = list(args)
    # BuildBuddy raw_json for the pinned Bazel 9.2.0 CI command includes one
    # trailing empty element in OptionsParsed.explicitCmdLine. The requested
    # target pattern is checked separately from PatternExpanded events.
    if values and values[-1] == "":
        values.pop()
    if any(not value for value in values):
        return None
    if values and values[0] in {"bazel", "bazelisk"}:
        values.pop(0)
    if values and values[0] == "test":
        values.pop(0)
    return Counter(values) if values else None


def _has_effective_rbe_options(args: tuple[str, ...] | None, rbe_image: str) -> bool:
    """Require the RBE config and its final remote-execution settings in BEP."""
    if args is None:
        return False
    values = list(args)
    # BuildBuddy raw_json for Bazel 9.2.0 includes one trailing empty element
    # in OptionsParsed.cmdLine, as it does for explicitCmdLine.
    if values and values[-1] == "":
        values.pop()
    if not values or any(not arg for arg in values) or "--config=rbe" not in values:
        return False

    def last_value(name: str) -> str | None:
        prefix = f"--{name}="
        matches = [arg[len(prefix) :] for arg in values if arg.startswith(prefix)]
        return matches[-1] if matches else None

    fallback_flags = [
        arg
        for arg in values
        if arg in {"--remote_local_fallback", "--noremote_local_fallback"} or arg.startswith("--remote_local_fallback=")
    ]
    fallback_disabled = bool(
        fallback_flags and fallback_flags[-1] in {"--noremote_local_fallback", "--remote_local_fallback=false"}
    )
    return (
        last_value("remote_executor") == "grpcs://remote.buildbuddy.io"
        and last_value("spawn_strategy") == "remote"
        and last_value("extra_execution_platforms") == "//:rbe_linux_x64"
        and last_value("remote_default_exec_properties") == f"container-image=docker://{rbe_image}"
        and fallback_disabled
    )


def verified_test_summaries(
    invocation: Invocation | None,
    *,
    invocation_id_value: str,
    source_commit: str,
    current_commit: str,
    run_id: str,
    run_attempt: str,
    event_name: str,
    rbe_image: str | None,
) -> tuple[Mapping[str, list[TestSummary]], str | None]:
    """Validate the invocation once; return its unmerged summaries on success.

    This proof is deliberately tied to the normal push workflow's deterministic
    invocation id, checkout SHA, complete successful test command and exact RBE
    image/config flags. Any missing or unfamiliar field disables reuse.
    """
    if invocation is None:
        return {}, "test invocation is unavailable"
    if event_name != "push":
        return {}, "only push-CI evidence can be reused"
    if not invocation_id_value or not run_id or not run_attempt:
        return {}, "test invocation identity is incomplete"
    expected_id = str(invocation_id(run_id=run_id, attempt=run_attempt, role="test"))
    if invocation_id_value != expected_id:
        return {}, "test invocation id does not match this workflow run"
    if not source_commit or source_commit != current_commit or invocation.commit_sha != current_commit:
        return {}, "test invocation source revision does not match publication source"
    if not rbe_image:
        return {}, "effective RBE image is unknown"
    if not invocation.finished or not invocation.successful:
        return {}, "test invocation has no successful terminal BuildFinished event"
    if invocation.command != "test":
        return {}, "BuildBuddy stream is not a Bazel test invocation"
    if invocation.requested_patterns != ("//...",):
        return {}, "test invocation did not request the full workspace sweep"
    if invocation.explicit_cmd_line is None:
        return {}, "test invocation has no unambiguous explicit command line"
    actual_options = _normalized_explicit_args(invocation.explicit_cmd_line)
    if not any(actual_options == expected for expected in _explicit_ci_options(invocation_id_value, rbe_image)):
        return {}, "test invocation Bazel or RBE configuration differs from publication"
    if not _has_effective_rbe_options(invocation.cmd_line, rbe_image):
        return {}, "test invocation's effective RBE configuration differs from publication"

    summaries: dict[str, list[TestSummary]] = defaultdict(list)
    for summary in invocation.test_summaries:
        summaries[summary.label].append(summary)
    return summaries, None


def selector_is_covered(selector: str, summaries: Mapping[str, list[TestSummary]]) -> tuple[bool, str]:
    """Whether every literal test in one publication selector has one clean verdict."""
    labels = selector.split()
    if not labels or any(
        not _LABEL.fullmatch(label) or "..." in label or any(c in label for c in "*?[]") for label in labels
    ):
        return False, "selector is not a list of literal workspace test labels"
    for label in dict.fromkeys(labels):
        matching = summaries.get(label, [])
        if len(matching) != 1:
            return False, f"{label} has {len(matching)} test summaries"
        summary = matching[0]
        if not summary.configuration_id:
            return False, f"{label} has no configuration identity"
        if summary.status != "PASSED":
            return False, f"{label} has non-passing status {summary.status!r}"
        if summary.attempt_count != 1:
            return False, f"{label} has missing or repeated test attempts"
        # A cached one-attempt pass is valid: its action key still includes the
        # source and Bazel configuration that this decision checked above.
        if summary.cached_count is not None and summary.cached_count > 1:
            return False, f"{label} reports more cached results than test attempts"
    return True, "every requested literal test has one passing attempt"
