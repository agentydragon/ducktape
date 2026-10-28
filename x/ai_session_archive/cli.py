"""Run one Restic-to-Postgres archive ingestion cycle."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

import yaml

from x.ai_session_archive.database import make_engine, make_session_factory
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticRepository


def _configured_roots(cli_values: list[str], archive_env: str) -> list[str]:
    if cli_values:
        values = cli_values
    elif value := os.environ.get(archive_env):
        values = [value]
    else:
        return []
    roots = values
    if any(not Path(root).is_absolute() for root in roots):
        raise SystemExit(f"{archive_env} and root arguments must be absolute Restic paths")
    return roots


def _restic_environment(
    *, machine_id: str, credentials_file: Path | None, environment: Mapping[str, str]
) -> dict[str, str]:
    restic_environment = dict(environment)
    if credentials_file is None:
        return restic_environment

    credential_environment = {"RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD", "RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE"}
    if credential_environment.intersection(environment):
        raise ValueError("provide Restic credentials through --credentials-file or RESTIC_* environment variables")

    credentials = yaml.safe_load(credentials_file.read_text())
    if not isinstance(credentials, Mapping):
        raise ValueError("the credentials file must contain a YAML mapping")

    keys = ("restic_rest_username_" + machine_id, "restic_rest_password_" + machine_id, "restic_password")
    username = credentials.get(keys[0])
    rest_password = credentials.get(keys[1])
    repository_password = credentials.get(keys[2])
    if not isinstance(username, str) or not username:
        raise ValueError("the credentials file is missing a non-empty Restic credential for this machine")
    if not isinstance(rest_password, str) or not rest_password:
        raise ValueError("the credentials file is missing a non-empty Restic credential for this machine")
    if not isinstance(repository_password, str) or not repository_password:
        raise ValueError("the credentials file is missing a non-empty Restic credential for this machine")
    restic_environment.update(
        RESTIC_REST_USERNAME=username, RESTIC_REST_PASSWORD=rest_password, RESTIC_PASSWORD=repository_password
    )
    return restic_environment


def _restic_binary() -> str:
    for root in (os.environ.get("RUNFILES_DIR"), os.environ.get("TEST_SRCDIR")):
        if root:
            for workspace in ("_main", "ducktape"):
                candidate = Path(root) / workspace / "third_party/restic/restic_bin"
                if candidate.is_file():
                    return str(candidate)
    raise RuntimeError("pinned Restic binary is unavailable in Bazel runfiles")


def _parse_args(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine-id", default=os.environ.get("MACHINE_ID"), required="MACHINE_ID" not in os.environ)
    parser.add_argument(
        "--display-name",
        default=os.environ.get("MACHINE_DISPLAY_NAME") or os.environ.get("MACHINE_ID"),
        required="MACHINE_DISPLAY_NAME" not in os.environ and "MACHINE_ID" not in os.environ,
    )
    parser.add_argument(
        "--claude-root",
        action="append",
        default=[],
        help="absolute Restic node root for CLAUDE_CONFIG_DIR (session_backup.sh uses /.claude); repeat for more roots",
    )
    parser.add_argument(
        "--codex-root",
        action="append",
        default=[],
        help="absolute Restic node root for CODEX_HOME (session_backup.sh uses /.codex); repeat for more roots",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        help="YAML mapping of shared session-backup credentials; values are passed only to the Restic child process",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> None:
    args = _parse_args(arguments)
    if not os.environ.get("RESTIC_REPOSITORY"):
        raise SystemExit("RESTIC_REPOSITORY must point at one host's Restic repository")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL must configure the Postgres connection")
    claude_roots = _configured_roots(args.claude_root, "CLAUDE_ARCHIVE_ROOT")
    codex_roots = _configured_roots(args.codex_root, "CODEX_ARCHIVE_ROOT")
    if not claude_roots and not codex_roots:
        raise SystemExit("configure at least one --claude-root/--codex-root or archive-root environment variable")
    roots = tuple(
        [HarnessRoot(Harness.CLAUDE, path) for path in claude_roots]
        + [HarnessRoot(Harness.CODEX, path) for path in codex_roots]
    )
    engine = make_engine(database_url)
    try:
        source = ArchiveSource(
            machine_id=args.machine_id,
            display_name=args.display_name,
            restic=ResticRepository(
                binary=_restic_binary(),
                environment=_restic_environment(
                    machine_id=args.machine_id, credentials_file=args.credentials_file, environment=os.environ
                ),
            ),
            roots=roots,
        )
        report = SnapshotIngestor(make_session_factory(engine), source).ingest()
        print(json.dumps(asdict(report), sort_keys=True))
        if report.snapshots_incomplete != 0 or report.files_failed != 0:
            raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
