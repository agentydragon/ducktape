#!/usr/bin/env python3
"""Wrapper around crun that injects gVisor-compatible options and annotations.

Fixes two gVisor limitations:

1. **setgroups**: gVisor doesn't provide /proc/self/setgroups, which crun's
   deny_setgroups() tries to open. The run.oci.keep_original_groups=1 annotation
   tells crun to skip that call. This annotation is set in containers.conf for
   `podman run`, but buildah doesn't propagate it to intermediate build containers.

2. **keyring quota**: gVisor has a limited kernel keyring quota (~60-70 per session).
   By default, crun creates a new session keyring for each container, exhausting
   the quota after ~60 RUN steps. The --no-new-keyring flag prevents this.

This wrapper injects both fixes before exec'ing the real crun.
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

    os.execv(REAL_CRUN, [REAL_CRUN, *args])


if __name__ == "__main__":
    main()
