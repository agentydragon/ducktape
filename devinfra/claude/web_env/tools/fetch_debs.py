"""Fetch exact .deb packages from Ubuntu snapshot archives and PPAs.

Reads live-dpkg-versions.txt and downloads the exact package versions needed to
reproduce the live Claude Code web container. Works from any machine with
network access — no dpkg-repack or live container needed.

Package sources (in priority order):
1. Ubuntu snapshot archive (snapshot.ubuntu.com) — multiple dates searched
2. Ondrej PHP PPA (ppa.launchpadcontent.net/ondrej/php)
3. Deadsnakes Python PPA (ppa.launchpadcontent.net/deadsnakes/ppa)
4. Docker CE repo (download.docker.com)

Snapshot dates are searched newest-first. Since each Packages.gz only lists the
latest version per package at that point in time, multiple snapshots are needed
to cover packages that were at intermediate versions when the live container was
built.

Usage:
    bazel run //devinfra/claude/web_env/tools:fetch_debs
"""

import gzip
import logging
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from util.bazel.workspace import get_build_workspace_directory

logger = logging.getLogger(__name__)

# Snapshot dates to search (newest first). Multiple dates are needed because
# the Packages.gz index at any given date only has the latest version of each
# package — older point releases disappear as newer ones are published.
# When adding packages, you may need to add a snapshot date from when those
# specific versions were current.
SNAPSHOT_DATES = [
    "20260317T000000Z",  # latest security updates
    "20260220T000000Z",  # curl 10.6, sudo .24.04.1
    "20260101T000000Z",  # gcc-13 6ubuntu2, older point releases
    "20251201T000000Z",  # initial container build era
    "20251001T000000Z",  # fallback for older packages
]

SNAPSHOT_BASE_TEMPLATE = "https://snapshot.ubuntu.com/ubuntu/{}"
SUITES = ["noble", "noble-updates", "noble-security"]
COMPONENTS = ["main", "restricted", "universe", "multiverse"]

# PPA and external repo sources for packages not in the main archive.
# Each entry: (base_url, suite, components, arch)
EXTRA_SOURCES: list[tuple[str, str, list[str], str]] = [
    # Ondrej PHP PPA
    ("https://ppa.launchpadcontent.net/ondrej/php/ubuntu", "noble", ["main"], "amd64"),
    # Deadsnakes Python PPA
    ("https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu", "noble", ["main"], "amd64"),
    # Docker CE
    ("https://download.docker.com/linux/ubuntu", "noble", ["stable"], "amd64"),
]

# Maximum parallel downloads.
MAX_WORKERS = 8


def _url_read(url: str) -> bytes:
    """Fetch URL contents with a reasonable timeout."""
    resp = requests.get(url, headers={"User-Agent": "fetch_debs/1.0"}, timeout=60)
    resp.raise_for_status()
    return resp.content


def _parse_packages_index(raw: str) -> dict[tuple[str, str], str]:
    """Parse a Debian Packages index into {(package, version): filename}."""
    result: dict[tuple[str, str], str] = {}
    pkg = ver = fname = None
    for line in raw.splitlines():
        if line.startswith("Package: "):
            pkg = line[9:]
        elif line.startswith("Version: "):
            ver = line[9:]
        elif line.startswith("Filename: "):
            fname = line[10:]
        elif line == "" and pkg and ver and fname:
            result[(pkg, ver)] = fname
            pkg = ver = fname = None
    # Handle last entry without trailing blank line
    if pkg and ver and fname:
        result[(pkg, ver)] = fname
    return result


def _fetch_and_parse_index(
    base_url: str, suite: str, component: str, arch: str = "amd64"
) -> dict[tuple[str, str], str]:
    """Download and parse a Packages.gz index."""
    url = f"{base_url}/dists/{suite}/{component}/binary-{arch}/Packages.gz"
    try:
        compressed = _url_read(url)
        raw = gzip.decompress(compressed).decode("utf-8", errors="replace")
        return _parse_packages_index(raw)
    except Exception:
        logger.debug("Failed to fetch index: %s", url)
        return {}


def build_package_index(needed: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Build an index mapping (package, version) -> download URL.

    Searches multiple snapshot dates to find exact versions. Stops searching
    once all needed packages are found.
    """
    logger.info("Building package index from snapshots + PPAs...")
    index: dict[tuple[str, str], str] = {}
    remaining = set(needed)

    # Extra sources first (PPAs, Docker) — these don't change by snapshot date
    for base_url, suite, components, arch in EXTRA_SOURCES:
        for component in components:
            entries = _fetch_and_parse_index(base_url, suite, component, arch)
            for key, filename in entries.items():
                if key not in index:
                    index[key] = f"{base_url}/{filename}"
                    remaining.discard(key)

    if remaining:
        logger.info("After PPAs: %d/%d found, %d remaining", len(needed) - len(remaining), len(needed), len(remaining))

    # Search snapshot dates newest-first
    for snapshot_date in SNAPSHOT_DATES:
        if not remaining:
            break
        snapshot_base = SNAPSHOT_BASE_TEMPLATE.format(snapshot_date)
        found_this_date = 0
        for suite in SUITES:
            for component in COMPONENTS:
                entries = _fetch_and_parse_index(snapshot_base, suite, component)
                for key, filename in entries.items():
                    if key not in index:
                        index[key] = f"{snapshot_base}/{filename}"
                    if key in remaining:
                        remaining.discard(key)
                        found_this_date += 1
        if found_this_date > 0:
            logger.info(
                "Snapshot %s: found %d new packages (%d remaining)", snapshot_date, found_this_date, len(remaining)
            )

    logger.info(
        "Package index: %d entries, %d/%d needed packages found", len(index), len(needed) - len(remaining), len(needed)
    )
    return index


def get_base_image_packages() -> dict[str, str]:
    """Get the package list from ubuntu:24.04 base image via docker."""
    logger.info("Getting base image package list...")
    result = subprocess.run(
        ["docker", "run", "--rm", "docker.io/library/ubuntu:24.04", "dpkg-query", "-W", "-f=${Package}=${Version}\n"],
        capture_output=True,
        text=True,
        check=True,
    )
    pkgs: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        stripped = raw_line.strip()
        if "=" in stripped:
            pkg, ver = stripped.split("=", 1)
            pkgs[pkg] = ver
    logger.info("Base image: %d packages", len(pkgs))
    return pkgs


def parse_versions_file(versions_file: Path) -> dict[str, str]:
    """Parse live-dpkg-versions.txt into {package: version}."""
    pkgs: dict[str, str] = {}
    for raw_line in versions_file.read_text().splitlines():
        stripped = raw_line.strip()
        if "=" in stripped:
            pkg, ver = stripped.split("=", 1)
            pkgs[pkg] = ver
    return pkgs


def _resolve_key(pkg: str, ver: str, index: dict[tuple[str, str], str]) -> str | None:
    """Try to resolve a package in the index with various key formats."""
    key = (pkg, ver)
    if key in index:
        return index[key]

    # Try with :amd64 architecture qualifier
    key_arch = (f"{pkg}:amd64", ver)
    if key_arch in index:
        return index[key_arch]

    # Try without epoch (some packages have epoch in version)
    if ":" in ver:
        ver_no_epoch = ver.split(":", 1)[1]
        key_no_epoch = (pkg, ver_no_epoch)
        if key_no_epoch in index:
            return index[key_no_epoch]

    return None


def download_deb(url: str, dest: Path) -> Path:
    """Download a .deb file to dest directory. Returns the file path."""
    filename = url.rsplit("/", 1)[-1]
    filepath = dest / filename
    if filepath.exists():
        return filepath

    data = _url_read(url)
    filepath.write_bytes(data)
    return filepath


def fetch_debs(work_dir: Path) -> None:
    """Fetch all .deb packages needed to reproduce the live container.

    Reads live-dpkg-versions.txt, compares against ubuntu:24.04 base image,
    and downloads exact versions from snapshot archives and PPAs.
    """
    versions_file = work_dir / "live-dpkg-versions.txt"
    if not versions_file.exists():
        raise FileNotFoundError(f"{versions_file} not found")

    live_pkgs = parse_versions_file(versions_file)
    base_pkgs = get_base_image_packages()

    # Find packages to download (not in base or at different version)
    to_download: dict[str, str] = {pkg: ver for pkg, ver in live_pkgs.items() if base_pkgs.get(pkg) != ver}
    logger.info("Need %d packages (of %d total, %d in base)", len(to_download), len(live_pkgs), len(base_pkgs))

    # Build the set of needed (package, version) keys for targeted searching
    needed: set[tuple[str, str]] = set()
    for pkg, ver in to_download.items():
        needed.add((pkg, ver))
        # Also add variant keys so the index builder knows to look for them
        needed.add((f"{pkg}:amd64", ver))
        if ":" in ver:
            needed.add((pkg, ver.split(":", 1)[1]))

    # Build the package index
    index = build_package_index(needed)

    # Resolve download URLs
    resolved: dict[str, str] = {}  # pkg -> url
    missing: list[str] = []
    for pkg, ver in sorted(to_download.items()):
        url = _resolve_key(pkg, ver, index)
        if url:
            resolved[pkg] = url
        else:
            missing.append(f"{pkg}={ver}")

    if missing:
        logger.error("Cannot find %d packages in any configured source:", len(missing))
        for m in sorted(missing):
            logger.error("  %s", m)
        raise RuntimeError(f"{len(missing)} packages not found in any repository. See errors above.")

    logger.info("All %d packages resolved to download URLs", len(resolved))

    # Clean and create output directory
    debs_dir = work_dir / "local_debs"
    if debs_dir.exists():
        shutil.rmtree(debs_dir)
    debs_dir.mkdir()

    # Download in parallel
    logger.info("Downloading %d .deb files...", len(resolved))
    failed: list[str] = []
    downloaded = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(download_deb, url, debs_dir): pkg for pkg, url in resolved.items()}
        for future in as_completed(futures):
            pkg = futures[future]
            try:
                future.result()
                downloaded += 1
                if downloaded % 50 == 0:
                    logger.info("  %d/%d downloaded...", downloaded, len(resolved))
            except Exception:
                logger.exception("Failed to download %s", pkg)
                failed.append(pkg)

    if failed:
        raise RuntimeError(f"Failed to download {len(failed)} packages: {', '.join(sorted(failed))}")

    deb_count = len(list(debs_dir.glob("*.deb")))
    total_size_mb = sum(f.stat().st_size for f in debs_dir.glob("*.deb")) / (1024 * 1024)
    logger.info("Downloaded %d .deb files (%.0f MB) to local_debs/", deb_count, total_size_mb)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    work_dir = get_build_workspace_directory() / "devinfra/claude/web_env"
    fetch_debs(work_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
