#!/usr/bin/env python3
"""
diagnose_open_fds.py
--------------------

Requires sudo/root to inspect all processes. Provides a system-wide snapshot of
open file descriptor (FD) usage and highlights heavy consumers with detailed
breakdowns.
"""

from __future__ import annotations

import argparse
import collections
from collections import Counter
from collections.abc import Iterable
import os
from pathlib import Path
import pwd
import stat
import sys

FD_DIR = Path("/proc")


class ProcessInfo:
    """Aggregated FD metadata for a single process."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.comm = "<unknown>"
        self.cmdline = "<unknown>"
        self.uid = None
        self.username = "<unknown>"
        self.fd_count = 0
        self.fd_categories: Counter[str] = collections.Counter()
        self.fd_examples: dict[str, str] = {}
        self.errors: list[str] = []
        self.limit_soft = None
        self.limit_hard = None

    def record_fd(self, link_target: str) -> None:
        category, example = categorize_fd(link_target)
        self.fd_categories[category] += 1
        self.fd_examples.setdefault(category, example)
        self.fd_count += 1


def require_root() -> None:
    if os.geteuid() != 0:
        sys.exit("This script must be run as root (sudo).")


def list_process_dirs() -> Iterable[Path]:
    for entry in FD_DIR.iterdir():
        if entry.is_dir() and entry.name.isdigit():
            yield entry


def read_comm(proc: Path) -> str:
    try:
        return (proc / "comm").read_text().strip()
    except (PermissionError, FileNotFoundError, OSError):
        return "<unknown>"


def read_cmdline(proc: Path) -> str:
    try:
        data = (proc / "cmdline").read_bytes()
    except (PermissionError, FileNotFoundError, OSError):
        return "<unknown>"
    return " ".join(part for part in data.decode("utf-8", "replace").split("\0") if part)


def read_owner(proc: Path) -> tuple[int | None, str]:
    try:
        st = proc.stat()
    except (PermissionError, FileNotFoundError, OSError):
        return None, "<unknown>"
    uid = st.st_uid
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    return uid, user


def read_limits(proc: Path) -> tuple[int | None, int | None]:
    limits_path = proc / "limits"
    try:
        lines = limits_path.read_text().splitlines()
    except (PermissionError, FileNotFoundError, OSError):
        return None, None
    for line in lines:
        if "Max open files" in line:
            parts = line.split()
            # Example columns: "Max open files   1024    1048576 files"
            if len(parts) >= 4:
                soft = int(parts[3]) if parts[3].isdigit() else None
                hard = int(parts[4]) if parts[4].isdigit() else None
                return soft, hard
    return None, None


def categorize_fd(link_target: str) -> tuple[str, str]:
    if link_target.startswith("socket:["):
        return "socket", link_target
    if link_target.startswith("pipe:["):
        return "pipe", link_target
    if link_target.startswith("anon_inode:["):
        inner = link_target[len("anon_inode:[") : -1]
        return f"anon_inode:{inner}", link_target
    if link_target.startswith("memfd:"):
        return "memfd", link_target
    if link_target.startswith("inotify"):
        return "inotify", link_target
    if link_target.startswith("/dev/"):
        return "device", link_target
    if link_target.startswith("/proc/"):
        return "procfs", link_target
    if link_target.startswith("/sys/"):
        return "sysfs", link_target
    if link_target.startswith("/") or link_target.startswith("."):
        return "path", link_target
    if link_target == "":
        return "unknown", "<empty>"
    return "other", link_target


def gather_process_info(pid_dir: Path) -> ProcessInfo | None:
    pid = int(pid_dir.name)
    info = ProcessInfo(pid)
    info.comm = read_comm(pid_dir)
    info.cmdline = read_cmdline(pid_dir)
    info.uid, info.username = read_owner(pid_dir)
    info.limit_soft, info.limit_hard = read_limits(pid_dir)

    fd_path = pid_dir / "fd"
    try:
        entries = list(fd_path.iterdir())
    except (PermissionError, FileNotFoundError, OSError) as exc:
        info.errors.append(f"fd_iter:{exc}")
        return info

    for entry in entries:
        try:
            link_target = os.readlink(entry)
        except OSError as exc:
            info.errors.append(f"fd:{entry.name}:{exc.errno}")
            continue
        info.record_fd(link_target)

    return info


def check_fd_dir_access() -> bool:
    try:
        st = Path("/proc/self/fd").stat()
    except PermissionError:
        return False
    # Ensure we can read symlinks; if not, the script will still work but warn.
    fd0 = Path("/proc/self/fd/0")
    try:
        os.readlink(fd0)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def format_process_summary(info: ProcessInfo) -> str:
    percent_of_limit = ""
    if info.limit_soft:
        percent = (info.fd_count / info.limit_soft) * 100
        percent_of_limit = f" ({percent:.1f}% of soft limit {info.limit_soft})"
    return f"{info.fd_count:6} FDs | pid={info.pid} | user={info.username} | {info.comm}{percent_of_limit}"


def format_category_breakdown(info: ProcessInfo, top: int = 6) -> str:
    items = info.fd_categories.most_common(top)
    parts = [f"{category}:{count}" for category, count in items]
    if len(info.fd_categories) > top:
        parts.append("…")
    return ", ".join(parts) if parts else "no descriptors"


def summarize_by_user(infos: Iterable[ProcessInfo]) -> list[tuple[int, str]]:
    counts: dict[str, int] = collections.Counter()
    for info in infos:
        counts[info.username] += info.fd_count
    return sorted(((count, user) for user, count in counts.items()), reverse=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose open file descriptor usage system-wide.")
    parser.add_argument(
        "--top", type=int, default=10, help="Number of top processes to display in detailed output (default: 10)."
    )
    parser.add_argument(
        "--pid", type=int, action="append", help="Inspect these specific PIDs regardless of ranking (can be repeated)."
    )
    parser.add_argument(
        "--min-fds", type=int, default=0, help="Only display processes with at least this many FDs in the summary."
    )
    return parser.parse_args()


def main() -> None:
    require_root()
    args = parse_args()

    if not check_fd_dir_access():
        print("WARNING: Limited visibility into /proc/<pid>/fd; some descriptors may be missing.", file=sys.stderr)

    infos: list[ProcessInfo] = []
    for proc_dir in list_process_dirs():
        info = gather_process_info(proc_dir)
        if info is None:
            continue
        infos.append(info)

    infos.sort(key=lambda i: i.fd_count, reverse=True)

    total_fds = sum(info.fd_count for info in infos)
    print(f"Total visible open FDs: {total_fds}")
    print("Top processes by open FDs:")
    for info in infos[: args.top]:
        print("  " + format_process_summary(info))
        print("    " + format_category_breakdown(info))
        if info.fd_examples:
            example_parts = [f"{category} -> {example}" for category, example in list(info.fd_examples.items())[:3]]
            print("    examples: " + "; ".join(example_parts))
        if info.errors:
            print("    errors: " + ", ".join(info.errors))

    if args.pid:
        requested = dict.fromkeys(args.pid)
        for info in infos:
            if info.pid in requested:
                requested[info.pid] = info
        for pid, info in requested.items():
            if info is None:
                print(f"\nPID {pid}: no data (process exited or inaccessible)")
                continue
            print(f"\nDetailed breakdown for PID {pid} ({info.comm}):")
            print(f"  Command line: {info.cmdline}")
            print(f"  Owner: {info.username} (uid={info.uid})")
            soft = info.limit_soft or "unknown"
            hard = info.limit_hard or "unknown"
            print(f"  Limits (soft/hard): {soft} / {hard}")
            print(f"  Total FDs: {info.fd_count}")
            for category, count in info.fd_categories.most_common():
                example = info.fd_examples.get(category, "<none>")
                print(f"    {category:<20} {count:5} example: {example}")
            if info.errors:
                print(f"  Errors encountered: {', '.join(info.errors)}")

    print("\nFD usage aggregated by user:")
    for count, user in summarize_by_user(infos):
        if count < args.min_fds:
            continue
        print(f"  {count:6} FDs | user={user}")

    if args.min_fds > 0:
        print(f"\nProcesses below {args.min_fds} FDs were omitted from the user summary.")


if __name__ == "__main__":
    main()
