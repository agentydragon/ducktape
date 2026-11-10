"""Identify already-published content requested by an exact devel CI source.

Release timestamps describe first publication, not promotion: A -> B -> A must
select A again. No evidence from an older source or workflow attempt is reused.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from devinfra.buildbuddy.ci import bes
from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.ci.artifacts import Artifact, artifacts_from_json
from devinfra.ci.plan_releases import Release, content_hash, content_tag, releases_from_json
from devinfra.ci.publication_test_verdicts import rbe_image_from_json

REPOSITORY = "agentydragon/ducktape"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def current_run(runs: Iterable[Any], source_commit: str) -> Any | None:
    """Select the latest attempt of this source's newest trusted push run.

    Select before checking completion. A pending rerun must not expose an older
    attempt, and a late old-source run must not overtake a newer no-op promotion.
    """
    matching = [
        run
        for run in runs
        if run.head_sha == source_commit
        and run.head_branch == "devel"
        and run.event == "push"
        and run.path == ".github/workflows/ci.yml"
        and run.head_repository.full_name == REPOSITORY
    ]
    if not matching:
        return None
    return max(matching, key=lambda run: (run.id, run.run_attempt))


def publication_source(repo: Any, checkout_sha: str) -> tuple[str, Any] | None:
    """Find the last CI-eligible first-parent source through intentional skips.

    A missing run for an ordinary commit may be delayed or deleted metadata;
    never interpret that as permission to promote an older source. A skipped
    commit remains unbuilt, even if it changes code. No-op CI sources still
    take precedence over late older runs without writing a tracked watermark.
    """
    workflow = repo.get_workflow("ci.yml")
    source = checkout_sha
    for _ in range(32):
        run = current_run(workflow.get_runs(branch="devel", event="push", head_sha=source), source)
        if run is not None and not (run.status == "completed" and run.conclusion == "skipped"):
            return (source, run) if run.status == "completed" and run.conclusion != "cancelled" else None
        commit = repo.get_commit(source)
        if "[skip ci]" not in commit.commit.message.lower() or not commit.parents:
            return None
        source = commit.parents[0].sha
    return None


@dataclasses.dataclass(frozen=True)
class Configuration:
    releases: list[Release]
    artifacts: list[Artifact]
    rbe_image: str | None


def source_configuration(repo: Any, source_commit: str) -> Configuration:
    """Load all promotion inputs at the selected immutable SHA, without checkout."""
    paths = ("devinfra/ci/artifact_targets.json", "skills/skills_registry.json", "devinfra/image_pins.json")

    def read(path: str) -> str:
        content = repo.get_contents(path, ref=source_commit).decoded_content
        if not isinstance(content, bytes):
            raise TypeError("source document did not contain bytes")
        return content.decode()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        targets, skills, image_pins = pool.map(read, paths)
    return Configuration(
        releases_from_json(targets, skills), artifacts_from_json(targets, skills), rbe_image_from_json(image_pins)
    )


def _options(args: tuple[str, ...] | None, command: str) -> Counter[str] | None:
    if args is None:
        return None
    values = list(args)
    if values and values[-1] == "":
        values.pop()  # Bazel 9's OptionsParsed includes one empty trailing value.
    if values and values[0] in {"bazel", "bazelisk"}:
        values.pop(0)
    if values and values[0] == command:
        values.pop(0)
    return Counter(values) if values and all(values) else None


def job_attempts(jobs: Iterable[Any], *, run_attempt: int) -> tuple[str, dict[str, str]]:
    """Use only GitHub's latest-job view, preserving each carried job's attempt.

    Never combine historical job lists: a full rerun's matrix may omit a package
    that existed in an older attempt. A newer matrix invalidates that old row.
    A replacement job that failed or is pending also suppresses its old result.
    """
    grouped: dict[str, list[Any]] = {}
    for job in jobs:
        grouped.setdefault(job.name, []).append(job)
    latest: dict[str, Any] = {}
    for name, rows in grouped.items():
        if any(not isinstance(row.run_attempt, int) or row.run_attempt < 1 for row in rows):
            continue
        attempt = max(row.run_attempt for row in rows)
        matching = [row for row in rows if row.run_attempt == attempt]
        if attempt <= run_attempt and len(matching) == 1:
            latest[name] = matching[0]
    normal = latest.get("bazel-ci / Test & Build")
    if normal and normal.conclusion == "cancelled":
        return "", {}  # ci.yml does not run publication after a cancelled bazel-ci job.
    normal_attempt = (
        str(normal.run_attempt)
        if normal and normal.status == "completed" and normal.conclusion in {"success", "failure", "timed_out"}
        else ""
    )
    matrix = latest.get("release / Compute release matrix")
    releases = {
        name: str(job.run_attempt)
        for name, job in latest.items()
        if name.startswith("release / Release ")
        and job.status == "completed"
        and job.conclusion == "success"
        and (job.run_attempt == run_attempt or (matrix and job.run_attempt >= matrix.run_attempt))
    }
    return normal_attempt, releases


def _release_flags(flags: str) -> list[str]:
    """Bazel canonicalizes -c opt and omits Starlark options from explicitCmdLine."""
    values = iter(shlex.split(flags))
    result = []
    for value in values:
        if value == "-c":
            result.append("--compilation_mode=" + next(values))
        elif value.startswith("--@"):
            continue
        else:
            result.append(value)
    return result


def verified_build(
    invocation: bes.Invocation | None,
    *,
    identity: str,
    source_commit: str,
    rbe_image: str,
    release: Release | None = None,
) -> bool:
    """Verify one unmerged stream's identity, source, requested targets and flags.

    A terminal failed workspace build may still announce unchanged, previously
    published artifacts. The planner already skips those releases without a new
    test gate. A per-release build must succeed and is also gated by its job.
    """
    if (
        invocation is None
        or not invocation.finished
        or invocation.commit_sha != source_commit
        or invocation.command != "build"
        or not rbe_image
    ):
        return False
    patterns = tuple(release.targets.split()) if release else ("//...",)
    if invocation.requested_patterns != patterns:
        return False
    common = ["--config=rbe", "--config=ci", f"--remote_default_exec_properties=container-image=docker://{rbe_image}"]
    if release:
        if not invocation.successful:
            return False
        expected = Counter(
            [
                *common,
                f"--invocation_id={identity}",
                "--config=buildbuddy_remote_cache",
                "--config=buildbuddy_bes_results_url",
                "--config=buildbuddy_bes_backend",
                "--remote_header=<REDACTED>",
                "--remote_upload_local_results",
                *_release_flags(release.bazel_flags),
            ]
        )
        return _options(invocation.explicit_cmd_line, "build") in (expected, expected + Counter(patterns))
    expected = Counter(
        [*common, "--keep_going", "--build_metadata=ROLE=CI", "--build_metadata=DISABLE_TARGET_TRACKING=false"]
    )
    return any(
        _options(invocation.explicit_cmd_line, "build") == expected + Counter(extra)
        for extra in ([], [f"--invocation_id={identity}"], ["//..."], [f"--invocation_id={identity}", "//..."])
    )


def desired_tags(
    releases: list[Release],
    *,
    source_commit: str,
    run_id: str,
    attempt: str,
    rbe_image: str,
    successful_jobs: Mapping[str, str],
    read: Callable[[str], bes.Invocation | None],
) -> dict[str, str]:
    """Resolve each group independently; missing evidence never selects a fallback source."""
    build_id = str(invocation_id(run_id=run_id, attempt=attempt, role="build"))
    normal = read(build_id) if attempt else None
    if not verified_build(normal, identity=build_id, source_commit=source_commit, rbe_image=rbe_image):
        normal = None
    by_label = normal.by_label() if normal else {}

    def digest(release: Release, invocation: bes.Invocation | None) -> str | None:
        if invocation is None:
            return None
        outputs = invocation.by_label()
        for target, _filename in release.assets:
            output = bes.artifact_output(outputs, target)
            if isinstance(output, str) or not _DIGEST.fullmatch(output.digest):
                return None
        try:
            return content_hash(release, outputs)
        except ValueError:
            return None

    def select(release: Release) -> tuple[str, str | None]:
        value = digest(release, normal) if release.uses_default_config and by_label else None
        if value is None and (own_attempt := successful_jobs.get(f"release / Release {release.pkg}")):
            identity = str(
                invocation_id(run_id=run_id, attempt=own_attempt, role="release-unstamped", scope=release.pkg)
            )
            own = read(identity)
            if verified_build(
                own, identity=identity, source_commit=source_commit, rbe_image=rbe_image, release=release
            ):
                value = digest(release, own)
        return release.pkg, content_tag(release.pkg, value) if value else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return {pkg: tag for pkg, tag in pool.map(select, releases) if tag is not None}


def read_build(identity: str) -> bes.Invocation | None:
    try:
        return bes.read(identity, timeout=45)
    except bes.BuildBuddyError:
        # Do not log the backend error: it can carry private invocation details.
        print("Build evidence unavailable; retaining affected pins")
        return None
