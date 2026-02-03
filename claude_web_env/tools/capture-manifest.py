#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Capture a full filesystem manifest as NDJSON.

Captures EVERYTHING — no skip directories. Exclusions are applied only at diff
time so manifests never need recapturing when exclusion rules change.

Uses a thread pool to parallelize SHA256 hashing for ~3-5x speedup on
I/O-heavy filesystems.

Usage:
    ./capture-manifest.py > manifest.ndjson
    ./capture-manifest.py /some/root > manifest.ndjson
"""

import grp
import hashlib
import os
import pwd
import stat
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from manifest import Entry, write_entry

MAX_HASH_SIZE = 50 * 1024 * 1024  # 50 MB
HASH_WORKERS = 8  # parallel hash threads

_uid_cache: dict[int, str] = {}
_gid_cache: dict[int, str] = {}


def uid_to_name(uid: int) -> str:
    if uid not in _uid_cache:
        try:
            _uid_cache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _uid_cache[uid] = str(uid)
    return _uid_cache[uid]


def gid_to_name(gid: int) -> str:
    if gid not in _gid_cache:
        try:
            _gid_cache[gid] = grp.getgrgid(gid).gr_name
        except KeyError:
            _gid_cache[gid] = str(gid)
    return _gid_cache[gid]


def sha256_file(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with Path(path).open("rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


TYPE_CHARS = {
    stat.S_IFREG: "f",
    stat.S_IFDIR: "d",
    stat.S_IFLNK: "l",
    stat.S_IFIFO: "p",
    stat.S_IFSOCK: "s",
    stat.S_IFBLK: "b",
    stat.S_IFCHR: "c",
}


def file_type_char(mode: int) -> str:
    return TYPE_CHARS.get(stat.S_IFMT(mode), "?")


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "/"
    strip_prefix = root.rstrip("/") if root != "/" else ""
    out = sys.stdout
    count = 0
    errors = 0

    # Batch entries then flush — submit hash futures for files, write
    # results in order to keep output deterministic.
    batch_size = 500

    with ThreadPoolExecutor(max_workers=HASH_WORKERS) as pool:
        # Each batch item: (entry_without_hash, hash_future_or_None)
        batch: list[tuple[Entry, Future[str | None] | None]] = []

        def flush_batch() -> None:
            nonlocal count
            for entry, future in batch:
                if future is not None:
                    entry.sha256 = future.result()
                write_entry(entry, out)
                count += 1
                if count % 10000 == 0:
                    print(f"  {count:,} entries processed...", file=sys.stderr)
            batch.clear()

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dirnames.sort()

            entries = [dirpath] + [str(Path(dirpath) / n) for n in sorted(filenames)]
            for path in entries:
                try:
                    lst = os.lstat(path)
                except OSError:
                    errors += 1
                    continue

                mode = lst.st_mode
                ftype = file_type_char(mode)
                size = lst.st_size if stat.S_ISREG(mode) else 0

                link_target: str | None = None
                hash_future: Future[str | None] | None = None

                if stat.S_ISLNK(mode):
                    try:
                        link_target = str(Path(path).readlink())
                    except OSError:
                        link_target = None
                elif stat.S_ISREG(mode) and size <= MAX_HASH_SIZE:
                    hash_future = pool.submit(sha256_file, path)

                recorded_path = path[len(strip_prefix) :] if strip_prefix else path
                if not recorded_path:
                    recorded_path = "/"

                entry = Entry(
                    path=recorded_path,
                    type=ftype,
                    perms=f"{stat.S_IMODE(mode):o}",
                    owner=uid_to_name(lst.st_uid),
                    group=gid_to_name(lst.st_gid),
                    size=size,
                    link_target=link_target,
                )
                batch.append((entry, hash_future))

                if len(batch) >= batch_size:
                    flush_batch()

        flush_batch()

    print(f"Done: {count:,} entries, {errors} errors", file=sys.stderr)


if __name__ == "__main__":
    main()
