"""Run one Restic-to-Postgres archive ingestion cycle."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from x.ai_session_archive.database import initialize_schema, make_engine, make_session_factory
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
        help="absolute CLAUDE_CONFIG_DIR path as stored in Restic; repeat for additional roots",
    )
    parser.add_argument(
        "--codex-root",
        action="append",
        default=[],
        help="absolute CODEX_HOME path as stored in Restic; repeat for additional roots",
    )
    parser.add_argument(
        "--initialize-fresh-database",
        action="store_true",
        help="create the prototype schema in a brand-new database; this does not upgrade an existing schema",
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
        if args.initialize_fresh_database:
            initialize_schema(engine)
        source = ArchiveSource(
            machine_id=args.machine_id,
            display_name=args.display_name,
            restic=ResticRepository(binary=_restic_binary(), environment=os.environ),
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
