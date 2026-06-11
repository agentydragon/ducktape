#!/usr/bin/env python3
"""Sync all files from an ez Share WiFi SD card to a local directory.

Brings up the host NetworkManager profile that joins the card's open AP, walks
the card's HTTP file index, downloads anything new or stale, then tears the
profile back down.
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from x.cpap_sync.card import EZShareClient

DEFAULT_BASE = "http://192.168.4.1"
DEFAULT_OUTPUT = Path("/data/cpap")
DEFAULT_NM_CONNECTION = "cpap-ezshare"

logger = logging.getLogger(__name__)


def sync(client: EZShareClient, output_dir: Path) -> None:
    for entry in client.walk():
        dest = EZShareClient.local_path(output_dir, entry.img_url)
        if dest.is_file() and (st := dest.stat()).st_size == entry.size and int(st.st_mtime) == entry.create_time:
            print(f"skip  {dest}")
            continue
        print(f"get   {dest}", flush=True)
        client.download(entry.img_url, dest)
        os.utime(dest, (entry.create_time, entry.create_time))
        print(f"done  {dest} ({dest.stat().st_size} bytes)", flush=True)


def nm_up(connection: str) -> None:
    result = subprocess.run(["nmcli", "connection", "up", connection], check=False)
    if result.returncode != 0:
        sys.exit(f"ERROR: Failed to bring up {connection!r}. Is the CPAP powered on and in range?")


def nm_down(connection: str) -> None:
    subprocess.run(["nmcli", "connection", "down", connection], check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--nm-connection", default=DEFAULT_NM_CONNECTION)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    nm_up(args.nm_connection)
    try:
        client = EZShareClient(args.base_url)
        print(f"Syncing card -> {args.output_dir}", flush=True)
        sync(client, args.output_dir)
        print("Sync complete.", flush=True)
    finally:
        nm_down(args.nm_connection)


if __name__ == "__main__":
    main()
