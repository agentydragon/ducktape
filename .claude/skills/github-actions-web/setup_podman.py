#!/usr/bin/env python3
"""Setup podman for running act in Claude Code on the web's gVisor container.

This configures podman with vfs storage driver and starts the podman service.
All operations use stdlib only - no external dependencies needed.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PODMAN_SOCKET = "/tmp/podman.sock"
CA_BUNDLE_DEST = "/tmp/ca-bundle.pem"
ACT_INSTALL_PATH = "/root/.local/bin/act"

# Known CA bundle locations in order of preference
CA_BUNDLE_LOCATIONS = [
    "/root/.cache/bazel-proxy/combined_ca.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    os.environ.get("SSL_CERT_FILE", ""),
    os.environ.get("REQUESTS_CA_BUNDLE", ""),
]

STORAGE_CONF = """\
[storage]
driver = "vfs"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"

[storage.options.vfs]
ignore_chown_errors = "true"
"""


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, check=check, **kwargs)


def command_exists(cmd: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(cmd) is not None


def install_podman() -> None:
    """Install podman if not present."""
    if command_exists("podman"):
        return
    print("Installing podman...")
    run(["apt-get", "update"])
    run(["apt-get", "install", "-y", "podman"])


def setup_subuid_subgid() -> None:
    """Add root to subuid/subgid for user namespace mapping."""
    for path in ["/etc/subuid", "/etc/subgid"]:
        content = Path(path).read_text() if Path(path).exists() else ""
        if not content.startswith("root:"):
            with Path(path).open("a") as f:
                f.write("root:100000:65536\n")


def configure_storage() -> None:
    """Configure podman with vfs storage driver (overlay doesn't work in gVisor)."""
    Path("/etc/containers").mkdir(parents=True, exist_ok=True)
    Path("/etc/containers/storage.conf").write_text(STORAGE_CONF)


def start_podman_service() -> None:
    """Kill any existing podman and start fresh."""
    # Kill existing podman processes
    run(["pkill", "-9", "podman"], check=False)
    time.sleep(1)

    # Start podman service
    subprocess.Popen(
        ["podman", "system", "service", "--time=0", f"unix://{PODMAN_SOCKET}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def find_ca_bundle() -> str | None:
    """Find CA bundle from known locations."""
    for loc in CA_BUNDLE_LOCATIONS:
        if loc and Path(loc).is_file():
            return loc
    return None


def copy_ca_bundle() -> str | None:
    """Copy CA bundle to /tmp for container mounting."""
    ca_bundle = find_ca_bundle()
    if ca_bundle:
        shutil.copy(ca_bundle, CA_BUNDLE_DEST)
        print(f"CA bundle copied from {ca_bundle} to {CA_BUNDLE_DEST}")
        return CA_BUNDLE_DEST
    print("WARNING: No CA bundle found. TLS connections may fail.")
    print(f"Searched: {', '.join(loc for loc in CA_BUNDLE_LOCATIONS if loc)}")
    return None


def install_act() -> None:
    """Install act if not present."""
    if command_exists("act") or Path(ACT_INSTALL_PATH).is_file():
        return
    print("Installing act...")
    install_script = subprocess.run(
        ["curl", "-fsSL", "https://raw.githubusercontent.com/nektos/act/master/install.sh"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(["bash", "-s", "--", "-b", "/root/.local/bin"], input=install_script.stdout, text=True, check=True)


def main() -> int:
    """Main setup routine."""
    script_dir = Path(__file__).parent

    print("=== Setting up podman for gVisor ===")

    install_podman()
    setup_subuid_subgid()
    configure_storage()
    start_podman_service()
    ca_bundle = copy_ca_bundle()
    install_act()

    # Set environment variables (for when this script is sourced/exec'd)
    os.environ["DOCKER_HOST"] = f"unix://{PODMAN_SOCKET}"
    if ca_bundle:
        os.environ["ACT_CA_BUNDLE"] = ca_bundle

    print()
    print("=== Setup complete ===")
    print(f"Podman socket: {os.environ['DOCKER_HOST']}")
    print(f"CA bundle: {ca_bundle or 'NOT FOUND'}")
    print()
    print("Next steps:")
    print("  1. Pull runner image: podman pull docker.io/catthehacker/ubuntu:act-latest")
    print(f"  2. Run jobs: {script_dir}/run_act.py pre-commit")

    return 0


if __name__ == "__main__":
    sys.exit(main())
