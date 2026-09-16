"""Emit machine-readable linkage for a GitHub Actions `bb remote` step."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CARET_ANSI_RE = re.compile(r"\^\[\[[0-9;]*m")
PROBE_CAS_RE = re.compile(r"CI_VM_PROBE_CAS (?P<rest>.*)")
BUILD_TOOL_LOG_NAMES = ["command.profile.gz", "critical path", "elapsed time", "process stats"]


@dataclasses.dataclass(frozen=True)
class ParsedLog:
    bazel_invocations: list[dict[str, Any]]
    probe_cas: list[dict[str, str]]
    warnings: list[str]


GITHUB_ENV_KEYS = [
    ("server_url", "GITHUB_SERVER_URL"),
    ("repository", "GITHUB_REPOSITORY"),
    ("workflow", "GITHUB_WORKFLOW"),
    ("run_id", "GITHUB_RUN_ID"),
    ("run_attempt", "GITHUB_RUN_ATTEMPT"),
    ("job", "GITHUB_JOB"),
    ("event_name", "GITHUB_EVENT_NAME"),
    ("ref", "GITHUB_REF"),
    ("sha", "GITHUB_SHA"),
    ("head_ref", "GITHUB_HEAD_REF"),
    ("base_ref", "GITHUB_BASE_REF"),
]


def strip_ansi(line: str) -> str:
    return CARET_ANSI_RE.sub("", ANSI_RE.sub("", line))


def split_roles(roles: str) -> list[str]:
    return [role.strip() for role in roles.split(",") if role.strip()]


def parse_log(text: str) -> ParsedLog:
    probe_cas: list[dict[str, str]] = []

    for raw in text.splitlines():
        line = strip_ansi(raw)
        if match := PROBE_CAS_RE.search(line):
            rest = match.group("rest")
            if rest.startswith("digest="):
                digest = rest.removeprefix("digest=")
                if digest:
                    probe_cas.append({"digest": digest})
                else:
                    probe_cas.append({"missing_digest": "empty"})
            elif rest.startswith("missing_digest="):
                probe_cas.append({"missing_digest": rest.removeprefix("missing_digest=")})
            else:
                probe_cas.append({"raw": rest})

    return ParsedLog(bazel_invocations=[], probe_cas=probe_cas, warnings=[])


def bazel_invocations(roles: list[str], invocation_ids: list[str]) -> list[dict[str, Any]]:
    """Build linkage from IDs supplied by the caller, never from command logs."""
    if len(roles) != len(invocation_ids):
        raise ValueError(f"expected one invocation id per role, got {len(roles)} roles and {len(invocation_ids)} ids")
    return [
        {"index": i, "role": role, "invocation_id": invocation_id, "build_tool_log_names": BUILD_TOOL_LOG_NAMES}
        for i, (role, invocation_id) in enumerate(zip(roles, invocation_ids, strict=True))
    ]


def github_linkage(env: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for output_key, env_key in GITHUB_ENV_KEYS:
        value = env.get(env_key)
        if value is not None:
            result[output_key] = value
    return result


def buildbuddy_linkage(parsed: ParsedLog) -> dict[str, Any]:
    return {"bazel_invocations": parsed.bazel_invocations, "probe_cas": parsed.probe_cas}


def write_step_output(record: Mapping[str, Any], github_output: str | None) -> None:
    """Publish the inner Bazel invocation ids so later jobs can read that build.

    They are the handle onto everything the build produced — its outputs, their
    content digests, and its per-target test verdicts all live in BuildBuddy under
    these ids, which is what lets a downstream job consult the build instead of
    repeating it.
    """
    if not github_output:
        return
    ids = ",".join(
        invocation["invocation_id"]
        for invocation in record["buildbuddy"]["bazel_invocations"]
        if invocation.get("invocation_id")
    )
    with Path(github_output).open("a") as f:
        f.write(f"bazel_invocation_ids={ids}\n")


def build_record(
    *,
    log_text: str,
    log_path: Path,
    roles: list[str],
    env: Mapping[str, str],
    bb_remote_exit_code: int | None,
    invocation_ids: list[str] | None = None,
) -> dict[str, Any]:
    parsed = parse_log(log_text)
    known_invocation_ids = invocation_ids or []
    warnings = list(parsed.warnings)
    if roles and not known_invocation_ids:
        warnings.append("child Bazel invocation ids not supplied")
    elif known_invocation_ids:
        parsed = dataclasses.replace(parsed, bazel_invocations=bazel_invocations(roles, known_invocation_ids))
    parsed = dataclasses.replace(parsed, warnings=warnings)
    record: dict[str, Any] = {
        "schema": "ducktape.bb_remote_linkage.v1",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "github": github_linkage(env),
        "buildbuddy": buildbuddy_linkage(parsed),
        "source_log_path": str(log_path),
        "warnings": parsed.warnings,
    }
    if bb_remote_exit_code is not None:
        record["bb_remote_exit_code"] = bb_remote_exit_code
    return record


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--roles", default="", help="Comma-separated roles for child Bazel invocations")
    p.add_argument(
        "--bazel-invocation-ids",
        default="",
        help="Comma-separated Bazel invocation ids supplied by the caller; never read from logs",
    )
    p.add_argument("--bb-remote-exit-code", type=int)
    return p


def main() -> None:
    args = parser().parse_args()
    log_text = args.log.read_text(errors="replace")
    record = build_record(
        log_text=log_text,
        log_path=args.log,
        roles=split_roles(args.roles),
        env=os.environ,
        bb_remote_exit_code=args.bb_remote_exit_code,
        invocation_ids=[i for i in args.bazel_invocation_ids.split(",") if i] if args.bazel_invocation_ids else None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(args.out)
    write_step_output(record, os.environ.get("GITHUB_OUTPUT"))


if __name__ == "__main__":
    main()
