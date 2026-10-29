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
            "--config=rbe",
            "--config=ci",
            "--build_metadata=ROLE=CI",
            "--build_metadata=DISABLE_TARGET_TRACKING=false",
            f"--remote_default_exec_properties=container-image=docker://{rbe_image}",
        ]
    )
    invocation_id_arg = Counter([f"--invocation_id={invocation_id_value}"])
    pattern_arg = Counter(["//..."])
    return base, base + invocation_id_arg, base + pattern_arg, base + invocation_id_arg + pattern_arg


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


def _push_invocation_problem(
    invocation: Invocation,
    *,
    invocation_id_value: str,
    source_commit: str,
    current_commit: str,
    run_id: str,
    run_attempt: str,
    event_name: str,
    rbe_image: str | None,
) -> str | None:
    """Why this invocation is not the normal push workflow's own full-sweep test of this source."""
    if event_name != "push":
        return "only push-CI evidence can be reused"
    if not invocation_id_value or not run_id or not run_attempt:
        return "test invocation identity is incomplete"
    expected_id = str(invocation_id(run_id=run_id, attempt=run_attempt, role="test"))
    if invocation_id_value != expected_id:
        return "test invocation id does not match this workflow run"
    if not source_commit or source_commit != current_commit or invocation.commit_sha != current_commit:
        return "test invocation source revision does not match publication source"
    if not rbe_image:
        return "effective RBE image is unknown"
    if invocation.command != "test":
        return "BuildBuddy stream is not a Bazel test invocation"
    if invocation.requested_patterns != ("//...",):
        return "test invocation did not request the full workspace sweep"
    if invocation.explicit_cmd_line is None:
        return "test invocation has no unambiguous explicit command line"
    actual_options = _normalized_explicit_args(invocation.explicit_cmd_line)
    if not any(actual_options == expected for expected in _explicit_ci_options(invocation_id_value, rbe_image)):
        return "test invocation Bazel or RBE configuration differs from publication"
    return None


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
    problem = _push_invocation_problem(
        invocation,
        invocation_id_value=invocation_id_value,
        source_commit=source_commit,
        current_commit=current_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        event_name=event_name,
        rbe_image=rbe_image,
    )
    if problem is not None:
        return {}, problem
    if not invocation.finished or not invocation.successful:
        return {}, "test invocation has no successful terminal BuildFinished event"

    summaries: dict[str, list[TestSummary]] = defaultdict(list)
    for summary in invocation.test_summaries:
        summaries[summary.label].append(summary)
    return summaries, None


# Verdicts a publication job's own `bazel test` of the same source and configuration repeats:
# the test ran (or its build failed) deterministically. Infrastructure outcomes such as
# REMOTE_FAILURE or INCOMPLETE keep the job's gate, which may succeed on retry.
_REPEATABLE_FAILURES = frozenset({"FAILED", "TIMEOUT", "FAILED_TO_BUILD"})


def recorded_test_failures(
    invocation: Invocation | None,
    *,
    invocation_id_value: str,
    source_commit: str,
    current_commit: str,
    run_id: str,
    run_attempt: str,
    event_name: str,
    rbe_image: str | None,
) -> frozenset[str]:
    """Literal test labels whose verified push-CI verdict on this source is a repeatable failure.

    The invocation only needs to have finished, not succeeded: a failed sweep is exactly
    when these verdicts matter. Identity and configuration checks are the ones reuse makes.
    """
    if invocation is None or not invocation.finished:
        return frozenset()
    problem = _push_invocation_problem(
        invocation,
        invocation_id_value=invocation_id_value,
        source_commit=source_commit,
        current_commit=current_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        event_name=event_name,
        rbe_image=rbe_image,
    )
    if problem is not None:
        return frozenset()
    return frozenset(summary.label for summary in invocation.test_summaries if summary.status in _REPEATABLE_FAILURES)


def selector_already_failed(selector: str, failed_labels: frozenset[str]) -> tuple[bool, str]:
    """Whether a literal test in one publication selector already failed in push CI on this source."""
    failed = [label for label in dict.fromkeys(selector.split()) if label in failed_labels]
    if failed:
        return True, f"{', '.join(failed)} failed in bazel-ci on this source"
    return False, "no recorded bazel-ci failure"


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
