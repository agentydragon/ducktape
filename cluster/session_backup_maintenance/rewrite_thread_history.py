"""Stage and verify removal of Codex's thread-history projection from Restic snapshots.

The original snapshots are never forgotten by this program.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

DATABASE = "thread_history_1.sqlite"
SUFFIXES = ("", "-wal", "-shm", "-journal")
ROOTS = ("/.codex/", "/.local/state/ducktape/ai-session-backup-sqlite/images/codex/*/")
PATTERNS = tuple(root + DATABASE + suffix for root in ROOTS for suffix in SUFFIXES)
ALLOWED = re.compile(
    r"^/(?:\.codex/|\.local/state/ducktape/ai-session-backup-sqlite/images/codex/[^/]+/)"
    r"thread_history_1\.sqlite(?:-wal|-shm|-journal)?$"
)


def restic(*args: str) -> str:
    return subprocess.run(["restic", *args], check=True, text=True, stdout=subprocess.PIPE).stdout


def repository_id() -> str:
    identifier = json.loads(restic("cat", "config"))["id"]
    if not isinstance(identifier, str):
        raise ValueError("invalid repository ID")
    return identifier


def snapshots() -> list[dict[str, Any]]:
    data = json.loads(restic("snapshots", "--json"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("invalid snapshot inventory")
    return cast(list[dict[str, Any]], data)


def exclusions() -> list[str]:
    return [argument for pattern in PATTERNS for argument in ("--exclude", pattern)]


def plan(path: Path) -> None:
    if path.exists():
        raise ValueError(f"plan already exists: {path}")
    baseline = snapshots()
    if not baseline:
        raise ValueError("repository contains no snapshots")
    ids = [snapshot["id"] for snapshot in baseline]
    # Restrict even the preview to the captured inventory; never rely on the
    # default of rewriting every snapshot in a changing repository.
    preview = restic("rewrite", "--dry-run", *exclusions(), *ids)
    path.write_text(json.dumps({"version": 1, "repository": repository_id(), "snapshots": ids}, indent=2) + "\n")
    print(preview, end="")
    print(f"Recorded {len(ids)} original snapshot IDs in {path}")


def load_plan(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    if data.get("version") != 1 or data.get("repository") != repository_id():
        raise ValueError("plan version or repository ID does not match")
    ids = data["snapshots"]
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
        raise ValueError("plan must contain distinct full snapshot IDs")
    if len(set(ids)) != len(ids) or any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in ids):
        raise ValueError("plan must contain distinct full snapshot IDs")
    return cast(list[str], ids)


def stage(path: Path) -> None:
    ids = load_plan(path)
    current = snapshots()
    if not set(ids).issubset({row["id"] for row in current}):
        raise ValueError("a planned original snapshot is missing")
    rewritten = [row["original"] for row in current if row.get("original") in ids and "rewrite" in row.get("tags", [])]
    if len(rewritten) != len(set(rewritten)):
        raise ValueError("multiple rewrites exist for an original; investigate before retrying")
    pending = [snapshot_id for snapshot_id in ids if snapshot_id not in rewritten]
    if not pending:
        print("All planned snapshots already have replacements; run verify.")
        return
    # Keep originals for verification and manual approval; never use --forget.
    print(restic("rewrite", *exclusions(), *pending), end="")


def verify(path: Path, output: Path) -> None:
    if output.exists():
        raise ValueError(f"verification report already exists: {output}")
    ids = load_plan(path)
    rows = snapshots()
    by_id = {row["id"]: row for row in rows}
    if not set(ids).issubset(by_id):
        raise ValueError("a planned original snapshot is missing")
    replacements: dict[str, str] = {}
    for row in rows:
        original = row.get("original")
        if original not in ids or "rewrite" not in row.get("tags", []):
            continue
        if original in replacements:
            raise ValueError(f"multiple rewrites for {original}")
        before = by_id[original]
        for key in ("time", "hostname", "username", "uid", "gid", "paths", "parent"):
            if row.get(key) != before.get(key):
                raise ValueError(f"snapshot {original} changed {key}")
        if sorted(row.get("tags", [])) != sorted([*before.get("tags", []), "rewrite"]):
            raise ValueError(f"snapshot {original} changed tags")
        changes = [json.loads(line) for line in restic("diff", "--json", original, row["id"]).splitlines()]
        changes = [change for change in changes if change.get("message_type") == "change"]
        if not changes or any(
            change.get("modifier") != "-" or not ALLOWED.fullmatch(change["path"]) for change in changes
        ):
            raise ValueError(f"unexpected diff for snapshot {original}")
        replacements[original] = row["id"]
    if not replacements:
        raise ValueError("no verified replacement snapshots were found")
    output.write_text(json.dumps({"repository": repository_id(), "replacements": replacements}, indent=2) + "\n")
    print(f"Verified {len(replacements)} replacements; originals are still retained. Report: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "stage", "verify"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("plan", type=Path)
        if name == "verify":
            subparser.add_argument("report", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        plan(args.plan)
    elif args.command == "stage":
        stage(args.plan)
    else:
        verify(args.plan, args.report)


if __name__ == "__main__":
    main()
