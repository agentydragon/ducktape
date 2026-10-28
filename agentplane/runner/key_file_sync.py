#!/usr/bin/python3
"""Refresh runner key files from Kubernetes Secrets through the Sandbox kubeconfig."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RETRY_SECONDS = 10


def _refresh_one(key_file: dict[str, Any], *, group_id: int | None) -> None:
    namespace = key_file["namespace"]
    secret_name = key_file["secret_name"]
    secret_key = key_file["secret_key"]
    destination = Path(key_file["mount_path"]) / key_file["file_name"]
    result = subprocess.run(
        [
            "kubectl",
            "--request-timeout=10s",
            "--namespace",
            namespace,
            "get",
            "secret",
            secret_name,
            "--output",
            "json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"kubectl exited with status {result.returncode}")
    encoded = json.loads(result.stdout)["data"][secret_key]
    value = base64.b64decode(encoded, validate=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(value)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o440)
            if group_id is not None:
                os.fchown(temporary.fileno(), -1, group_id)
        Path(temporary_name).replace(destination)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _refresh_all(config_path: Path, *, group_id: int | None) -> bool:
    try:
        key_files = json.loads(config_path.read_text(encoding="utf-8"))
        for key_file in key_files:
            _refresh_one(key_file, group_id=group_id)
    except Exception:
        # Never log subprocess output or Secret data.
        logger.warning("Could not refresh runner key files")
        return False
    return True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="JSON list of Secret names and file paths")
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--group-id", type=int, help="Set the output file's group, for VM harness uid 107")
    parser.add_argument("--once", action="store_true", help="Fetch each key once and exit")
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args()
    if args.once:
        if not _refresh_all(args.config, group_id=args.group_id):
            # The init container/systemd ExecStartPre uses this status to gate runner startup.
            raise SystemExit(1)
        return

    while True:
        interval = args.interval_seconds
        if not _refresh_all(args.config, group_id=args.group_id):
            interval = min(interval, _RETRY_SECONDS)
        time.sleep(interval)


if __name__ == "__main__":
    main()
