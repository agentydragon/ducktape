"""Supplement raw harness-directory backups with consistent SQLite images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

SQLITE_HEADER = b"SQLite format 3\x00"


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(source: Path, destination: Path, digest: str) -> None:
    # Do not replace identical files: inode/ctime changes defeat Restic's
    # --skip-if-unchanged even when mtime and content are unchanged.
    if destination.is_file() and file_digest(destination) == digest:
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".snapshot-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            shutil.copyfileobj(original, target)
            target.flush()
            os.fsync(target.fileno())
        os.utime(temporary, ns=(0, 0))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def databases(root: Path, on_error: Callable[[OSError], None]) -> Iterator[Path]:
    # SQLite databases need not have a .sqlite suffix. Do not follow symlinks
    # outside the captured tree or block on pipes/sockets while probing headers.
    if not root.is_dir():
        raise FileNotFoundError(f"harness directory does not exist: {root}")
    for directory, subdirectories, filenames in os.walk(root, onerror=on_error, followlinks=False):
        subdirectories.sort()
        for filename in sorted(filenames):
            path = Path(directory, filename)
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                with path.open("rb") as stream:
                    is_database = stream.read(len(SQLITE_HEADER)) == SQLITE_HEADER
                if is_database:
                    yield path
            except OSError as error:
                on_error(error)


def snapshot_databases(roots: Mapping[str, Path], output_dir: Path) -> list[str]:
    for name, root in roots.items():
        if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
            raise ValueError(f"invalid harness name: {name!r}")
        if output_dir.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"snapshot output must be outside the harness directory: {root}")

    images = []
    errors: list[str] = []
    # Temporary database writes stay outside the backed-up tree. Its files and
    # directories are mutated only when an image or the manifest actually changes.
    with tempfile.TemporaryDirectory(prefix="ai-session-backup-") as temporary_directory:
        temporary = Path(temporary_directory)
        for name, configured_root in sorted(roots.items()):
            root = configured_root.absolute()
            try:
                for database in databases(root, lambda error: errors.append(str(error))):
                    image = temporary / "database.sqlite"
                    image.unlink(missing_ok=True)
                    relative = database.relative_to(root)
                    # Include root identity so changing CODEX_HOME cannot alias an
                    # existing image belonging to another source directory.
                    root_identity = hashlib.sha256(os.fsencode(root)).hexdigest()
                    destination = output_dir / "images" / name / root_identity / relative
                    try:
                        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
                        try:
                            backup = sqlite3.connect(image)
                            try:
                                source.backup(backup)
                            finally:
                                backup.close()
                        finally:
                            source.close()
                        digest = file_digest(image)
                        publish(image, destination, digest)
                        images.append(
                            {
                                "harness": name,
                                "source": str(database),
                                "image": str(destination.relative_to(output_dir)),
                                "sha256": digest,
                            }
                        )
                    except (OSError, sqlite3.Error) as error:
                        errors.append(f"{database}: {error}")
            except OSError as error:
                errors.append(str(error))

        # Prior images remain locally available if their sources disappear or a
        # later capture fails. Only this manifest's entries are current successes.
        manifest = temporary / "manifest.json"
        manifest.write_text(json.dumps({"version": 1, "databases": images, "errors": errors}, sort_keys=True) + "\n")
        publish(manifest, output_dir / "manifest.json", file_digest(manifest))
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", nargs=2, action="append", metavar=("HARNESS", "DIRECTORY"), required=True)
    args = parser.parse_args()
    roots = {name: Path(directory) for name, directory in args.root}
    if len(roots) != len(args.root):
        parser.error("harness names must be unique")
    errors = snapshot_databases(roots, args.output)
    for error in errors:
        print(f"SQLite snapshot failed: {error}", file=sys.stderr)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
