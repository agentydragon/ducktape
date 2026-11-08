"""Back up session trees and consistent SQLite images with Restic."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("restic")
    parser.add_argument("snapshot_script")
    parser.add_argument("home", type=Path)
    parser.add_argument("claude_home", type=Path)
    parser.add_argument("codex_home", type=Path)
    parser.add_argument("staging_dir", type=Path)
    parser.add_argument("host")
    args = parser.parse_args()

    environment = os.environ | {"TMPDIR": os.environ["RUNTIME_DIRECTORY"]}
    snapshot_excludes: list[str] = []
    backup_excludes: list[str] = []

    def exclude(root: Path, harness: str, relative: str, *, directory: bool = False) -> None:
        snapshot_excludes.extend(("--exclude-directory" if directory else "--exclude-file", str(root / relative)))
        # Staging is persistent: exclude images created before these rules as well.
        backup_excludes.extend(
            (
                "--exclude",
                str(root / relative),
                "--exclude",
                str(args.staging_dir / "images" / harness / "*" / relative),
            )
        )

    # Hook-generated build state, worktrees, caches, packages and diagnostic logs.
    for relative in ("session-env", ".tmp", "cache", ".cache", "plugins/cache", "debug", "logs"):
        exclude(args.claude_home, "claude", relative, directory=True)
    for relative in ("worktrees", ".tmp", "tmp", "packages", "cache", ".cache", "plugins/cache", "log", "logs"):
        exclude(args.codex_home, "codex", relative, directory=True)
    for relative in (
        "stats-cache.json",
        "mcp-needs-auth-cache.json",
        "plugins/plugin-directory-cache-v2.json",
        "daemon.log",
    ):
        exclude(args.claude_home, "claude", relative)
    exclude(args.codex_home, "codex", "models_cache.json")
    # Codex projects paginated history from the durable rollout JSONL:
    # https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/thread-store/src/local/live_writer.rs#L349-L364
    # logs_2.sqlite contains diagnostic logs, not conversation history.
    for database in ("logs_2.sqlite", "thread_history_1.sqlite"):
        exclude(args.codex_home, "codex", database)
        for suffix in ("-wal", "-shm", "-journal"):
            backup_excludes.extend(
                (
                    "--exclude",
                    str(args.codex_home / (database + suffix)),
                    "--exclude",
                    str(args.staging_dir / "images/codex/*" / (database + suffix)),
                )
            )

    # Preserve raw files even when a damaged database cannot produce a consistent
    # image. The persisted manifest records errors, and the unit still fails.
    snapshot = subprocess.run(
        [
            sys.executable,
            args.snapshot_script,
            "--output",
            str(args.staging_dir),
            "--root",
            "claude",
            str(args.claude_home),
            "--root",
            "codex",
            str(args.codex_home),
            *snapshot_excludes,
        ],
        cwd=args.home,
        env=environment,
        check=False,
    )
    if subprocess.run(
        [args.restic, "snapshots", "--latest", "1"],
        cwd=args.home,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode:
        subprocess.run([args.restic, "init"], cwd=args.home, env=environment, check=True)

    # Relative sources avoid recording changing /home and user-directory metadata.
    sources = [
        str(path).removeprefix(f"{args.home}/") for path in (args.claude_home, args.codex_home, args.staging_dir)
    ]
    subprocess.run(
        [
            args.restic,
            "backup",
            "--host",
            args.host,
            "--tag",
            "ai-sessions",
            "--skip-if-unchanged",
            *backup_excludes,
            "--",
            *sources,
        ],
        cwd=args.home,
        env=environment,
        check=True,
    )
    return snapshot.returncode


if __name__ == "__main__":
    sys.exit(main())
