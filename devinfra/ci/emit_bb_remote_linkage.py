# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0"]
# ///
"""Emit machine-readable linkage for a GitHub Actions `bb remote` step."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CARET_ANSI_RE = re.compile(r"\^\[\[[0-9;]*m")
RUNNER_INVOCATION_RE = re.compile(
    r"Streaming remote runner logs to:\s*(?P<url>https://[^\s]+/invocation/(?P<id>[0-9a-f-]{36}))"
)
BAZEL_INVOCATION_RE = re.compile(r"Invocation ID: (?P<id>[0-9a-f-]{36})")
PROBE_CAS_RE = re.compile(r"CI_VM_PROBE_CAS (?P<rest>.*)")


class BazelInvocation(BaseModel):
    index: int
    role: str
    invocation_id: str
    build_tool_log_names: list[str]


class ProbeCas(BaseModel):
    digest: str | None = None
    missing_digest: str | None = None
    raw: str | None = None


class ParsedLog(BaseModel):
    runner_invocation_id: str | None
    runner_invocation_url: str | None
    bazel_invocations: list[BazelInvocation]
    probe_cas: list[ProbeCas]
    warnings: list[str]


class GitHubLinkage(BaseModel):
    server_url: str | None
    repository: str | None
    workflow: str | None
    run_id: str | None
    run_attempt: str | None
    job: str | None
    event_name: str | None
    ref: str | None
    sha: str | None
    head_ref: str | None
    base_ref: str | None


class BuildBuddyLinkage(BaseModel):
    runner_invocation_id: str | None
    runner_invocation_url: str | None
    bazel_invocations: list[BazelInvocation]
    probe_cas: list[ProbeCas]


class LinkageRecord(BaseModel):
    schema_version: str = Field(default="ducktape.bb_remote_linkage.v1", serialization_alias="schema")
    created_at: str
    github: GitHubLinkage
    buildbuddy: BuildBuddyLinkage
    bb_remote_exit_code: int | None
    source_log_path: str
    warnings: list[str]


def strip_ansi(line: str) -> str:
    return CARET_ANSI_RE.sub("", ANSI_RE.sub("", line))


def split_roles(roles: str) -> list[str]:
    return [role.strip() for role in roles.split(",") if role.strip()]


def parse_log(text: str, roles: list[str]) -> ParsedLog:
    runner_invocation_id = None
    runner_invocation_url = None
    bazel_invocation_ids: list[str] = []
    probe_cas: list[ProbeCas] = []

    for raw in text.splitlines():
        line = strip_ansi(raw)
        if match := RUNNER_INVOCATION_RE.search(line):
            runner_invocation_id = match.group("id")
            runner_invocation_url = match.group("url")
            continue
        if match := BAZEL_INVOCATION_RE.search(line):
            invocation_id = match.group("id")
            if invocation_id not in bazel_invocation_ids:
                bazel_invocation_ids.append(invocation_id)
            continue
        if match := PROBE_CAS_RE.search(line):
            rest = match.group("rest")
            if rest.startswith("digest="):
                digest = rest.removeprefix("digest=")
                if digest:
                    probe_cas.append(ProbeCas(digest=digest))
                else:
                    probe_cas.append(ProbeCas(missing_digest="empty"))
            elif rest.startswith("missing_digest="):
                probe_cas.append(ProbeCas(missing_digest=rest.removeprefix("missing_digest=")))
            else:
                probe_cas.append(ProbeCas(raw=rest))

    bazel_invocations: list[BazelInvocation] = []
    for i, invocation_id in enumerate(bazel_invocation_ids):
        role = roles[i] if i < len(roles) else f"command-{i}"
        bazel_invocations.append(
            BazelInvocation(
                index=i,
                role=role,
                invocation_id=invocation_id,
                build_tool_log_names=["command.profile.gz", "critical path", "elapsed time", "process stats"],
            )
        )

    warnings = []
    if runner_invocation_id is None:
        warnings.append("runner invocation id not found")
    if not bazel_invocations:
        warnings.append("child Bazel invocation ids not found")

    return ParsedLog(
        runner_invocation_id=runner_invocation_id,
        runner_invocation_url=runner_invocation_url,
        bazel_invocations=bazel_invocations,
        probe_cas=probe_cas,
        warnings=warnings,
    )


def build_record(
    *, log_text: str, log_path: Path, roles: list[str], env: Mapping[str, str], bb_remote_exit_code: int | None
) -> dict[str, Any]:
    parsed = parse_log(log_text, roles)
    record = LinkageRecord(
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        github=GitHubLinkage(
            server_url=env.get("GITHUB_SERVER_URL"),
            repository=env.get("GITHUB_REPOSITORY"),
            workflow=env.get("GITHUB_WORKFLOW"),
            run_id=env.get("GITHUB_RUN_ID"),
            run_attempt=env.get("GITHUB_RUN_ATTEMPT"),
            job=env.get("GITHUB_JOB"),
            event_name=env.get("GITHUB_EVENT_NAME"),
            ref=env.get("GITHUB_REF"),
            sha=env.get("GITHUB_SHA"),
            head_ref=env.get("GITHUB_HEAD_REF"),
            base_ref=env.get("GITHUB_BASE_REF"),
        ),
        buildbuddy=BuildBuddyLinkage(
            runner_invocation_id=parsed.runner_invocation_id,
            runner_invocation_url=parsed.runner_invocation_url,
            bazel_invocations=parsed.bazel_invocations,
            probe_cas=parsed.probe_cas,
        ),
        bb_remote_exit_code=bb_remote_exit_code,
        source_log_path=str(log_path),
        warnings=parsed.warnings,
    )
    return record.model_dump(mode="json", by_alias=True, exclude_none=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--roles", default="", help="Comma-separated roles for child Bazel invocations")
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
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(args.out)


if __name__ == "__main__":
    main()
