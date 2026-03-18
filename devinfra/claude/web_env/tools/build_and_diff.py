"""Build the Claude Code web container and generate a diff report.

Uses Docker (docker build --network=host). Docker data-root must be on tmpfs
(configured at /mnt/bazel-tmpfs/docker via session hooks).

Usage:
    bazel run //devinfra/claude/web_env/tools:build_and_diff
    bazel run //devinfra/claude/web_env/tools:build_and_diff -- --diff-only
    bazel run //devinfra/claude/web_env/tools:build_and_diff -- --capture-binaries
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devinfra.claude.web_env.tools.capture_manifest import capture, write_manifest
from devinfra.claude.web_env.tools.capture_versions import capture_versions_yaml
from devinfra.claude.web_env.tools.diff_manifests import REAL_DIFF_STATUSES, diff_manifests, generate_report
from devinfra.claude.web_env.tools.fetch_debs import fetch_debs
from devinfra.claude.web_env.tools.manifest import Entry, load_default_exclusions
from util.bazel.workspace import get_build_workspace_directory

logger = logging.getLogger(__name__)

IMAGE_NAME = "claude_code_web_recreated"
CONTAINER_NAME = "capture_tmp"


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(cmd, check=True, **kwargs)


def capture_proprietary_binaries(work_dir: Path) -> None:
    logger.info("Capturing proprietary binaries from live container...")
    ref_dir = work_dir / "reference"

    # process_api runs as PID 1 and is only accessible via /proc/1/exe
    # (the /process_api path is not a regular file in gVisor).
    for src, name in [
        ("/usr/local/bin/environment-manager", "environment-manager.gz"),
        ("/proc/1/exe", "process_api.gz"),
    ]:
        with (ref_dir / name).open("wb") as f:
            run(["gzip", "-c", src], stdout=f)
        result = run(["sha256sum", src], capture_output=True, text=True)
        logger.info("Captured %s: %s", Path(src).name, result.stdout.split()[0])

    logger.info("Proprietary binaries captured to reference/")


def generate_local_debs(work_dir: Path) -> None:
    """Fetch .deb packages from Ubuntu snapshot archives and PPAs.

    Downloads exact package versions from pinned remote sources. Works from
    any machine with network access — no dpkg-repack or live container needed.
    """
    fetch_debs(work_dir)


def build_image(work_dir: Path) -> None:
    logger.info("Building Dockerfile with Docker...")
    https_proxy = os.environ.get("https_proxy", "")
    https_proxy_upper = os.environ.get("HTTPS_PROXY", https_proxy)
    no_proxy = os.environ.get("no_proxy", "")
    no_proxy_upper = os.environ.get("NO_PROXY", "")

    run(
        [
            "docker",
            "build",
            "--network=host",
            "--build-arg",
            f"http_proxy={https_proxy}",
            "--build-arg",
            f"https_proxy={https_proxy}",
            "--build-arg",
            f"HTTP_PROXY={https_proxy_upper}",
            "--build-arg",
            f"HTTPS_PROXY={https_proxy_upper}",
            "--build-arg",
            f"no_proxy={no_proxy}",
            "--build-arg",
            f"NO_PROXY={no_proxy_upper}",
            "-t",
            IMAGE_NAME,
            ".",
        ],
        cwd=work_dir,
    )
    logger.info("Build complete: %s", IMAGE_NAME)


def capture_live_manifest() -> dict[str, Entry]:
    logger.info("Capturing live manifest...")
    entries = capture()
    logger.info("Live manifest: %s entries", f"{len(entries):,}")
    return entries


def capture_built_manifest(work_dir: Path) -> dict[str, Entry]:
    logger.info("Capturing built manifest...")
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=False, capture_output=True)
    run(["docker", "create", "--name", CONTAINER_NAME, IMAGE_NAME, "/bin/true"])

    tmpdir = Path(tempfile.mkdtemp(prefix="built_rootfs_"))
    logger.info("Extracting built image filesystem to %s...", tmpdir)
    export_proc = subprocess.Popen(["docker", "export", CONTAINER_NAME], stdout=subprocess.PIPE)
    run(["tar", "-x", "--numeric-owner", "-C", str(tmpdir)], stdin=export_proc.stdout)
    export_proc.wait()
    run(["docker", "rm", CONTAINER_NAME])

    entries = capture(root=tmpdir)
    shutil.rmtree(tmpdir)
    logger.info("Built manifest: %s entries", f"{len(entries):,}")
    return entries


def generate_diff_report(work_dir: Path, live: dict[str, Entry], built: dict[str, Entry]) -> None:
    logger.info("Generating diff report...")
    excl = load_default_exclusions()

    results, pattern_hits = diff_manifests(live, built, excl)
    report = generate_report(results, "live", "built", pattern_hits, excl)
    real_diffs = sum(1 for r in results if r.status in REAL_DIFF_STATUSES)

    out_path = work_dir / "diff_report.md"
    out_path.write_text(report)
    logger.info("Diff report written to diff_report.md (%s real differences)", f"{real_diffs:,}")

    # Also write NDJSON manifests for reference
    with (work_dir / "live_manifest.ndjson").open("w") as f:
        write_manifest(live, f)
    with (work_dir / "built_manifest.ndjson").open("w") as f:
        write_manifest(built, f)

    # Print summary lines
    for line in report.splitlines()[:50]:
        if line.startswith(("#", "**")) or (line and line[0].isdigit()):
            print(line)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    parser = argparse.ArgumentParser(description="Build the Claude Code web container and generate a diff report.")
    parser.add_argument("--diff-only", action="store_true", help="Skip build, just regenerate diff")
    parser.add_argument("--capture-binaries", action="store_true", help="Capture proprietary binaries only")
    args = parser.parse_args()

    work_dir = get_build_workspace_directory() / "devinfra/claude/web_env"

    if args.capture_binaries:
        capture_proprietary_binaries(work_dir)
        return 0

    if not args.diff_only:
        capture_proprietary_binaries(work_dir)
        generate_local_debs(work_dir)
        build_image(work_dir)
    else:
        logger.info("Skipping build (--diff-only)")
        result = subprocess.run(["docker", "image", "inspect", IMAGE_NAME], check=False, capture_output=True)
        if result.returncode != 0:
            logger.error("Image %s not found. Run without --diff-only first.", IMAGE_NAME)
            return 1

    logger.info("Capturing version snapshot...")
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    versions_file = work_dir / "reference" / f"versions_{date_str}.yaml"
    versions_file.write_text(capture_versions_yaml())
    logger.info("Version snapshot saved to %s", versions_file.name)

    live = capture_live_manifest()
    built = capture_built_manifest(work_dir)
    generate_diff_report(work_dir, live, built)

    logger.info("Done! Review diff_report.md and commit if changes are expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
