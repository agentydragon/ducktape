#!/usr/bin/env python3
"""Sync all files from an ez Share WiFi SD card into the cpap-data Forgejo repo.

Partial-clones the repo, associates the host-network WiFi interface with the
card's AP, walks the card's HTTP file index, downloads anything new or changed
into the worktree, tears the WiFi session back down, then commits + pushes — git
operations never overlap the card-WiFi window.

A committed `sync_meta.json` manifest records each file's size and card
timestamp; a card entry matching its manifest entry is skipped (git discards
mtimes, so the manifest replaces the old PVC-era stat check). The recorded size
is the byte count actually stored at download time, so a file caught mid-write
mismatches the card's next listing and self-heals on the following run.
"""

import argparse
import contextlib
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from cpap.card import EZShareClient, FileEntry, download_relpath
from cpap.gitstore import GitStore

DEFAULT_BASE = "http://192.168.4.1"
DEFAULT_WIFI_INTERFACE = "wlx9cefd5f62ee0"
DEFAULT_WIFI_SSID = "Rai CPAP ez Share"
MANIFEST_FILENAME = "sync_meta.json"
AUTHOR_NAME = "cpap sync"
AUTHOR_EMAIL = "cpap@allegedly.works"
WIFI_ASSOCIATION_TIMEOUT = 60

logger = logging.getLogger(__name__)


class CardClient(Protocol):
    """The slice of EZShareClient that the sync consumes (tests substitute a fake)."""

    def walk(self) -> Iterator[FileEntry]: ...
    def download(self, url: str, dest: Path) -> None: ...


class FileMeta(BaseModel):
    size: int = Field(description="Bytes actually stored at download time.")
    create_time: int = Field(description="Card-reported unix timestamp.")


class SyncManifest(BaseModel):
    """Per-file card metadata, committed as sync_meta.json alongside the data."""

    files: dict[str, FileMeta] = Field(default_factory=dict, description="Repo-relative posix path -> metadata.")


def load_manifest(store: GitStore) -> SyncManifest:
    blob = store.head_blob(MANIFEST_FILENAME)
    return SyncManifest() if blob is None else SyncManifest.model_validate_json(blob)


def download_changed(client: CardClient, workdir: Path, manifest: SyncManifest) -> list[str]:
    """Download card files that are new or differ from their manifest entry.

    Enumerates the whole card first (cheap XML listings, no file bodies) so
    progress can be logged as a fraction of a known total — the first re-seed
    fetches thousands of files and bare per-file lines give no sense of how far
    along it is. Updates `manifest` in place and returns the downloaded
    repo-relative paths.
    """
    seen = 0
    pending: list[tuple[FileEntry, Path, str]] = []
    for entry in client.walk():
        seen += 1
        rel = download_relpath(entry.img_url)
        dest = workdir / rel
        if (meta := manifest.files.get(rel)) and meta.size == entry.size and meta.create_time == entry.create_time:
            continue
        pending.append((entry, dest, rel))

    total = len(pending)
    total_mib = sum(entry.size for entry, _, _ in pending) / 1024 / 1024
    logger.info("card: %d file(s), %d new/changed to fetch (%.1f MiB)", seen, total, total_mib)

    changed: list[str] = []
    done_mib = 0.0
    for i, (entry, dest, rel) in enumerate(pending, start=1):
        logger.info("[%d/%d %.0f%%, %.0f/%.0f MiB] get %s", i, total, 100 * i / total, done_mib, total_mib, rel)
        client.download(entry.img_url, dest)
        stored = dest.stat().st_size
        done_mib += stored / 1024 / 1024
        manifest.files[rel] = FileMeta(size=stored, create_time=entry.create_time)
        changed.append(rel)
    return changed


def _wpa_quote(value: str) -> str:
    """Quote a string for a wpa_supplicant double-quoted value."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _wpa_config(*, ssid: str, password: str, control_dir: Path) -> str:
    return f"""ctrl_interface=DIR={control_dir}
update_config=0
country=US

network={{
    ssid={_wpa_quote(ssid)}
    psk={_wpa_quote(password)}
    key_mgmt=WPA-PSK
}}
"""


def _wait_for_association(interface: str, timeout: int = WIFI_ASSOCIATION_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["iw", "dev", interface, "link"], check=False, capture_output=True, text=True)
        if result.returncode == 0 and "Connected to" in result.stdout:
            return
        time.sleep(1)
    raise RuntimeError(f"WiFi interface {interface!r} did not associate with the CPAP AP within {timeout}s")


def _remove_default_routes(interface: str) -> None:
    """Remove DHCP-installed default routes belonging to the CPAP interface."""
    for family in ("-4", "-6"):
        result = subprocess.run(
            ["ip", family, "route", "show", "default", "dev", interface], check=False, capture_output=True, text=True
        )
        for route in result.stdout.splitlines():
            subprocess.run(["ip", family, "route", "del", *route.split()], check=False)


@contextlib.contextmanager
def wifi_connection(*, interface: str, ssid: str, password: str, runtime_dir: Path) -> Iterator[None]:
    """Temporarily associate the host-network WiFi interface with the CPAP AP.

    Talos supplies the kernel driver and network interface. The container only
    supplies the userspace supplicant and DHCP client, and runs with hostNetwork
    so those tools operate on the node's interface.
    """
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = runtime_dir / "wpa_supplicant.conf"
    config.write_text(_wpa_config(ssid=ssid, password=password, control_dir=runtime_dir))
    config.chmod(0o600)
    process: subprocess.Popen[bytes] | None = None
    try:
        subprocess.run(["ip", "link", "set", "dev", interface, "up"], check=True)
        process = subprocess.Popen(
            ["wpa_supplicant", "-i", interface, "-c", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        _wait_for_association(interface)
        subprocess.run(["dhclient", "-1", "-v", interface], check=True)
        # The CPAP AP is a private side network. Do not let DHCP replace the
        # OptiPlex's default route or affect cluster/Forgejo connectivity.
        _remove_default_routes(interface)
        yield
    finally:
        subprocess.run(["dhclient", "-r", interface], check=False)
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        subprocess.run(["ip", "addr", "flush", "dev", interface], check=False)
        subprocess.run(["ip", "link", "set", "dev", interface, "down"], check=False)


def run_sync(
    *,
    client: CardClient,
    git_url: str,
    branch: str,
    wifi_interface: str | None,
    wifi_ssid: str,
    wifi_password: str | None,
    username: str | None,
    password: str | None,
    now: datetime,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = GitStore.clone(git_url, Path(tmp) / "repo", branch=branch, username=username, password=password)
        manifest = load_manifest(store)

        if wifi_interface is None:
            changed = download_changed(client, store.workdir, manifest)
        else:
            if wifi_password is None:
                raise ValueError("wifi_password is required when wifi_interface is set")
            with wifi_connection(
                interface=wifi_interface, ssid=wifi_ssid, password=wifi_password, runtime_dir=Path(tmp) / "wifi"
            ):
                changed = download_changed(client, store.workdir, manifest)

        if not changed:
            logger.info("card matches manifest; nothing to commit")
            return
        (store.workdir / MANIFEST_FILENAME).write_text(manifest.model_dump_json(indent=2) + "\n")
        store.stage([*changed, MANIFEST_FILENAME])
        if not store.has_staged_changes():
            # Possible when a re-download produced byte-identical content (e.g. a
            # mid-write file truncated at the same boundary twice).
            logger.info("downloads identical to committed blobs; nothing to commit")
            return
        store.commit(f"cpap: sync {now.date().isoformat()}", author_name=AUTHOR_NAME, author_email=AUTHOR_EMAIL)
        store.push()
        logger.info("pushed %d changed file(s) to %s", len(changed), branch)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--git-url", required=True, help="cpap-data repo URL (creds via GIT_USERNAME/GIT_PASSWORD).")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument(
        "--wifi-interface",
        default=DEFAULT_WIFI_INTERFACE,
        help="Host-network WiFi interface to associate with the card's AP ('' if already connected).",
    )
    ap.add_argument("--wifi-ssid", default=DEFAULT_WIFI_SSID, help="Card WiFi SSID.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_sync(
        client=EZShareClient(args.base_url),
        git_url=args.git_url,
        branch=args.branch,
        wifi_interface=args.wifi_interface or None,
        wifi_ssid=args.wifi_ssid,
        wifi_password=os.environ.get("CPAP_WIFI_PASSWORD"),
        username=os.environ["GIT_USERNAME"],
        password=os.environ["GIT_PASSWORD"],
        now=datetime.now(UTC),
    )


if __name__ == "__main__":
    main()
