#!/usr/bin/env python3
"""Wrapper around crun that injects gVisor-compatible OCI annotations.

gVisor doesn't provide /proc/self/setgroups, which crun's deny_setgroups()
tries to open. The run.oci.keep_original_groups=1 annotation tells crun to
skip that call. This annotation is set in containers.conf and works for
`podman run`, but buildah doesn't propagate it to intermediate build
containers. This wrapper injects it into config.json before exec'ing crun.
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


def main() -> None:
    args = sys.argv[1:]
    bundle_dir = find_bundle_dir(args)
    if bundle_dir is not None:
        inject_annotation(bundle_dir)

    os.execv(REAL_CRUN, [REAL_CRUN, *args])


if __name__ == "__main__":
    main()
