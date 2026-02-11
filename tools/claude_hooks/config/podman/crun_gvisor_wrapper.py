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

import json
import os
import sys
from pathlib import Path

REAL_CRUN = "/usr/bin/crun"
ANNOTATION_KEY = "run.oci.keep_original_groups"
ANNOTATION_VALUE = "1"


def find_bundle_dir(args: list[str]) -> Path | None:
    """Extract bundle directory from crun CLI arguments."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-b", "--bundle") and i + 1 < len(args):
            return Path(args[i + 1])
        if arg.startswith("--bundle="):
            return Path(arg.split("=", 1)[1])
        i += 1
    return None


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


FREEZER_BASE = Path("/sys/fs/cgroup/freezer")


def _find_container_id(args: list[str]) -> str | None:
    """Extract container ID from crun exec arguments.

    crun exec syntax: crun exec [options] <container-id> <command...>
    Options start with '-', so the container ID is the first non-option arg
    after the 'exec' command.
    """
    past_command = False
    i = 0
    while i < len(args):
        arg = args[i]
        if not past_command:
            if arg == "exec":
                past_command = True
            i += 1
            continue
        # Skip option flags and their values
        if arg.startswith("-"):
            # Flags that take a value argument
            if arg in (
                "--cwd",
                "--user",
                "-u",
                "--cap",
                "--env",
                "-e",
                "--apparmor",
                "--process",
                "-p",
                "--pid-file",
                "--console-socket",
                "--preserve-fds",
            ):
                i += 2
                continue
            i += 1
            continue
        return arg
    return None


def ensure_mock_freezer(args: list[str]) -> None:
    """Create mock cgroup freezer state file for exec commands.

    gVisor's /sys/fs/cgroup is a writable tmpfs but has no freezer subsystem.
    crun exec tries to freeze the container via freezer.state, failing with
    "No such file or directory". Creating a regular file at the expected path
    allows crun to write "FROZEN"/"THAWED" and proceed normally.
    """
    if not args or args[0] != "exec":
        return

    container_id = _find_container_id(args)
    if container_id is None:
        return

    # crun looks for the freezer at the container's cgroup path
    freezer_dir = FREEZER_BASE / "libpod_parent" / f"libpod-{container_id}"
    freezer_state = freezer_dir / "freezer.state"
    if freezer_state.exists():
        return

    freezer_dir.mkdir(parents=True, exist_ok=True)
    freezer_state.write_text("THAWED")


def inject_no_new_keyring(args: list[str]) -> list[str]:
    """Inject --no-new-keyring for create/run commands if not already present.

    This prevents gVisor keyring quota exhaustion. Each container would otherwise
    create a new session keyring, exhausting the ~60-70 keyring limit after that
    many RUN steps in a Dockerfile.
    """
    if not args:
        return args

    command = args[0]
    if command not in ("create", "run"):
        return args

    if "--no-new-keyring" in args:
        return args

    # Insert --no-new-keyring after the command name
    return [command, "--no-new-keyring", *args[1:]]


def main() -> None:
    args = sys.argv[1:]

    # Inject --no-new-keyring to prevent keyring quota exhaustion
    args = inject_no_new_keyring(args)

    # Inject OCI annotation for setgroups workaround
    bundle_dir = find_bundle_dir(args)
    if bundle_dir is not None:
        inject_annotation(bundle_dir)

    # Create mock cgroup freezer for exec commands
    ensure_mock_freezer(args)

    os.execv(REAL_CRUN, [REAL_CRUN, *args])


if __name__ == "__main__":
    main()
