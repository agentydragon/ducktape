#!/usr/bin/env python3
"""Wrapper around crun that injects gVisor-compatible options and annotations.

Fixes three gVisor limitations:

1. **setgroups**: gVisor doesn't provide /proc/self/setgroups, which crun's
   deny_setgroups() tries to open. The run.oci.keep_original_groups=1 annotation
   tells crun to skip that call. This annotation is set in containers.conf for
   `podman run`, but buildah doesn't propagate it to intermediate build containers.

2. **keyring quota**: gVisor has a limited kernel keyring quota (~60-70 per session).
   By default, crun creates a new session keyring for each container, exhausting
   the quota after ~60 RUN steps. The --no-new-keyring flag prevents this.

3. **cgroup freezer for exec**: gVisor doesn't provide the cgroup v1 freezer
   subsystem. crun's exec implementation tries to freeze the container via
   /sys/fs/cgroup/freezer/.../freezer.state before exec'ing into it. Since
   /sys/fs/cgroup is a writable tmpfs in gVisor, we create a mock freezer.state
   file that crun can write to, allowing exec to proceed.

This wrapper injects all fixes before exec'ing the real crun.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REAL_CRUN = "/usr/bin/crun"
ANNOTATION_KEY = "run.oci.keep_original_groups"
ANNOTATION_VALUE = "1"
FREEZER_BASE = Path("/sys/fs/cgroup/freezer")

CRUN_SUBCOMMANDS = frozenset(
    {
        "checkpoint",
        "create",
        "delete",
        "exec",
        "kill",
        "list",
        "pause",
        "ps",
        "restore",
        "resume",
        "run",
        "spec",
        "start",
        "state",
        "update",
    }
)


@dataclass(frozen=True)
class CrunArgs:
    """Parsed crun CLI arguments relevant to the gVisor wrapper."""

    command: str | None
    command_idx: int | None
    bundle_dir: Path | None
    has_no_new_keyring: bool
    container_id: str | None


def _build_create_run_parser() -> argparse.ArgumentParser:
    """Parser for create/run flags the wrapper needs."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-b", "--bundle")
    parser.add_argument("--no-new-keyring", action="store_true")
    return parser


def _build_exec_parser() -> argparse.ArgumentParser:
    """Parser for exec flags that take values.

    These must be declared so parse_known_args consumes their values instead of
    leaving them in the remainder (where the first non-flag item is taken as the
    container ID).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cwd")
    parser.add_argument("-u", "--user")
    parser.add_argument("--cap")
    parser.add_argument("-e", "--env", action="append")
    parser.add_argument("--apparmor")
    parser.add_argument("-p", "--process")
    parser.add_argument("--pid-file")
    parser.add_argument("--console-socket")
    parser.add_argument("--preserve-fds")
    return parser


def parse_crun_args(args: list[str]) -> CrunArgs:
    """Parse crun CLI arguments, extracting what the wrapper needs.

    Identifies the subcommand and extracts:
    - For create/run: --bundle path and --no-new-keyring presence
    - For exec: the container ID (first positional after options)
    """
    # Find subcommand: first arg matching a known crun command
    command = None
    command_idx = None
    for i, arg in enumerate(args):
        if arg in CRUN_SUBCOMMANDS:
            command_idx = i
            command = arg
            break

    if command is None or command_idx is None:
        return CrunArgs(command=None, command_idx=None, bundle_dir=None, has_no_new_keyring=False, container_id=None)

    subcommand_args = args[command_idx + 1 :]

    if command in ("create", "run"):
        known, _ = _build_create_run_parser().parse_known_args(subcommand_args)
        return CrunArgs(
            command=command,
            command_idx=command_idx,
            bundle_dir=Path(known.bundle) if known.bundle else None,
            has_no_new_keyring=known.no_new_keyring,
            container_id=None,
        )

    if command == "exec":
        _, rest = _build_exec_parser().parse_known_args(subcommand_args)
        container_id = next((arg for arg in rest if not arg.startswith("-")), None)
        return CrunArgs(
            command=command,
            command_idx=command_idx,
            bundle_dir=None,
            has_no_new_keyring=False,
            container_id=container_id,
        )

    return CrunArgs(
        command=command, command_idx=command_idx, bundle_dir=None, has_no_new_keyring=False, container_id=None
    )


def inject_annotation(bundle_dir: Path) -> None:
    """Inject keep_original_groups annotation into the OCI config.json."""
    config_path = bundle_dir / "config.json"
    if not config_path.exists():
        return

    config = json.loads(config_path.read_text())
    annotations = config.setdefault("annotations", {})
    if annotations.get(ANNOTATION_KEY) == ANNOTATION_VALUE:
        return

    annotations[ANNOTATION_KEY] = ANNOTATION_VALUE
    config_path.write_text(json.dumps(config))


def ensure_mock_freezer(container_id: str) -> None:
    """Create mock cgroup freezer state file for a container.

    gVisor's /sys/fs/cgroup is a writable tmpfs but has no freezer subsystem.
    crun exec tries to freeze the container via freezer.state, failing with
    "No such file or directory". Creating a regular file at the expected path
    allows crun to write "FROZEN"/"THAWED" and proceed normally.
    """
    freezer_dir = FREEZER_BASE / "libpod_parent" / f"libpod-{container_id}"
    freezer_state = freezer_dir / "freezer.state"
    if freezer_state.exists():
        return

    freezer_dir.mkdir(parents=True, exist_ok=True)
    freezer_state.write_text("THAWED")


def inject_no_new_keyring(args: list[str], parsed: CrunArgs) -> list[str]:
    """Inject --no-new-keyring for create/run commands if not already present.

    This prevents gVisor keyring quota exhaustion. Each container would otherwise
    create a new session keyring, exhausting the ~60-70 keyring limit after that
    many RUN steps in a Dockerfile.
    """
    if parsed.command not in ("create", "run"):
        return args
    if parsed.has_no_new_keyring:
        return args
    assert parsed.command_idx is not None
    idx = parsed.command_idx
    return [*args[: idx + 1], "--no-new-keyring", *args[idx + 1 :]]


def main() -> None:
    args = sys.argv[1:]
    parsed = parse_crun_args(args)

    args = inject_no_new_keyring(args, parsed)

    if parsed.bundle_dir is not None:
        inject_annotation(parsed.bundle_dir)

    if parsed.container_id is not None:
        ensure_mock_freezer(parsed.container_id)

    os.execv(REAL_CRUN, [REAL_CRUN, *args])


if __name__ == "__main__":
    main()
