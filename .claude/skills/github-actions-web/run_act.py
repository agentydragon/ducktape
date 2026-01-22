#!/usr/bin/env python3
"""Run act with all workarounds for Claude Code on the web's gVisor container.

Auto-detects CA bundle, proxy settings, and custom image if available.
All operations use stdlib only - no external dependencies needed.

Usage:
    ./run_act.py [job-name] [extra-act-args...]

Examples:
    ./run_act.py pre-commit
    ./run_act.py bazel-build --verbose
    ./run_act.py -l  # List jobs
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PODMAN_SOCKET = "/tmp/podman.sock"
CA_BUNDLE_DEST = "/tmp/ca-bundle.pem"
ACT_PATHS = ["/root/.local/bin/act"]
CUSTOM_IMAGE = "localhost/act-proxy:latest"
DEFAULT_IMAGE = "catthehacker/ubuntu:act-latest"

# Bazel proxy port (same as bazel_proxy_setup.py)
BAZEL_PROXY_PORT = 18081
BAZEL_PROXY_DIR = Path.home() / ".cache" / "bazel-proxy"
BAZEL_USER_BAZELRC = Path.home() / ".bazelrc"

# Known CA bundle locations in order of preference
CA_BUNDLE_LOCATIONS = [
    "/tmp/ca-bundle.pem",
    "/root/.cache/bazel-proxy/combined_ca.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    os.environ.get("SSL_CERT_FILE", ""),
    os.environ.get("REQUESTS_CA_BUNDLE", ""),
    os.environ.get("ACT_CA_BUNDLE", ""),
]


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, check=check, **kwargs)


def find_ca_bundle() -> str | None:
    """Find CA bundle from known locations."""
    for loc in CA_BUNDLE_LOCATIONS:
        if loc and Path(loc).is_file():
            return loc
    return None


def ensure_ca_bundle() -> str:
    """Ensure CA bundle is available at /tmp/ca-bundle.pem."""
    ca_bundle = find_ca_bundle()
    if not ca_bundle:
        print("ERROR: CA bundle not found. Run setup_podman.py first or set ACT_CA_BUNDLE.")
        sys.exit(1)

    # Copy to /tmp if not already there
    if ca_bundle != CA_BUNDLE_DEST:
        shutil.copy(ca_bundle, CA_BUNDLE_DEST)

    return CA_BUNDLE_DEST


def ensure_podman_running() -> None:
    """Ensure podman socket is running."""
    if Path(PODMAN_SOCKET).is_socket():
        return

    print("Podman socket not found. Starting podman service...")
    run(["pkill", "-9", "podman"], check=False)
    time.sleep(1)
    subprocess.Popen(
        ["podman", "system", "service", "--time=0", f"unix://{PODMAN_SOCKET}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def cleanup_containers() -> None:
    """Clean up any stale containers."""
    run(["podman", "rm", "--all", "--force"], check=False, capture_output=True)


def find_act() -> str:
    """Find act binary."""
    # Check explicit paths first
    for path in ACT_PATHS:
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path

    # Check PATH
    act_in_path = shutil.which("act")
    if act_in_path:
        return act_in_path

    print("ERROR: act not found. Run setup_podman.py first.")
    sys.exit(1)


def check_custom_image() -> tuple[str, bool]:
    """Check if custom act-proxy image exists."""
    result = run(["podman", "image", "exists", CUSTOM_IMAGE], check=False, capture_output=True)
    if result.returncode == 0:
        print("Using custom act-proxy:latest image (with global-agent)")
        return CUSTOM_IMAGE, True
    print("Using standard catthehacker/ubuntu:act-latest image")
    print("Note: Some Node.js actions may fail. Build act-proxy:latest for full support.")
    return DEFAULT_IMAGE, False


def is_bazel_proxy_running() -> bool:
    """Check if the bazel proxy is running on the expected port."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(("127.0.0.1", BAZEL_PROXY_PORT))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_proxy_env() -> dict[str, str]:
    """Get proxy environment variables from host environment.

    Container-level proxy should use the direct proxy from the environment.
    Bazel actions use the bazel proxy configured separately in .bazelrc.act.
    """
    # Mirror proxy configuration from host environment
    # The container uses --network=host so it can reach the same proxy
    env_vars = {}

    # Copy all proxy-related environment variables
    proxy_var_names = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "GLOBAL_AGENT_HTTP_PROXY",
        "GLOBAL_AGENT_HTTPS_PROXY",
        "GLOBAL_AGENT_NO_PROXY",
        "YARN_HTTP_PROXY",
        "YARN_HTTPS_PROXY",
    ]

    for var in proxy_var_names:
        value = os.environ.get(var, "")
        if value:
            env_vars[var] = value

    if env_vars.get("HTTP_PROXY"):
        print("Using proxy from environment")

    return env_vars


def build_act_command(
    act_bin: str, job: str, runner_image: str, use_local_image: bool, ca_bundle: str, extra_args: list[str]
) -> list[str]:
    """Build the act command with all workarounds."""
    proxy_env = get_proxy_env()

    cmd = [act_bin, "-j", job, "-P", f"ubuntu-latest={runner_image}"]

    # Don't pull if using local image
    if use_local_image:
        cmd.append("--pull=false")

    cmd.append("--network=host")

    # Add proxy environment variables
    for key, value in proxy_env.items():
        cmd.extend(["--env", f"{key}={value}"])

    # Build container options as a single combined string
    # (multiple --container-options may not be handled correctly by act)
    container_opts: list[str] = []

    # Custom act-proxy image has CA bundle baked in at /etc/ssl/certs/custom-ca-bundle.pem
    # Don't override those env vars - only set them for the default image
    if use_local_image:
        # Custom image: CA is at /etc/ssl/certs/custom-ca-bundle.pem (set in Dockerfile)
        # Just mount the bazel proxy config
        pass
    else:
        # Default image: need to mount and configure CA bundle
        ca_env_vars = ["NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"]
        for var in ca_env_vars:
            cmd.extend(["--env", f"{var}={ca_bundle}"])
        # Mount CA bundle into container
        container_opts.append(f"-v {ca_bundle}:{ca_bundle}:ro")

    # Mount bazel proxy configuration if available (for Bazel JVM settings)
    truststore = BAZEL_PROXY_DIR / "cacerts.jks"
    if BAZEL_PROXY_DIR.exists():
        container_opts.append(f"-v {BAZEL_PROXY_DIR}:{BAZEL_PROXY_DIR}:ro")
        print(f"Mounting bazel proxy config: {BAZEL_PROXY_DIR}")

        # Set JAVA_TOOL_OPTIONS to configure JVM truststore and proxy
        # Note: Bazel ignores JAVA_TOOL_OPTIONS but other Java tools may use it
        if truststore.exists() and is_bazel_proxy_running():
            java_opts = (
                f"-Dhttps.proxyHost=127.0.0.1 "
                f"-Dhttps.proxyPort={BAZEL_PROXY_PORT} "
                f"-Djavax.net.ssl.trustStore={truststore} "
                f"-Djavax.net.ssl.trustStorePassword=changeit"
            )
            cmd.extend(["--env", f"JAVA_TOOL_OPTIONS={java_opts}"])
            print("Setting JAVA_TOOL_OPTIONS for JVM tools")

    if BAZEL_USER_BAZELRC.exists():
        container_opts.append(f"-v {BAZEL_USER_BAZELRC}:{BAZEL_USER_BAZELRC}:ro")
        print(f"Mounting user bazelrc: {BAZEL_USER_BAZELRC}")

    # Add all container options as a single argument
    if container_opts:
        cmd.extend(["--container-options", " ".join(container_opts)])

    # Add extra args
    cmd.extend(extra_args)

    return cmd


def generate_workspace_bazelrc() -> None:
    """Generate a .bazelrc.act file in the workspace for Bazel proxy settings.

    This file is in the workspace so it gets copied into the act container.
    The project .bazelrc should include: try-import .bazelrc.act
    """
    workspace_bazelrc = Path.cwd() / ".bazelrc.act"

    # Check if bazel proxy is running
    if not is_bazel_proxy_running():
        # Clean up any stale file
        if workspace_bazelrc.exists():
            workspace_bazelrc.unlink()
        return

    # Check for Java truststore
    truststore = BAZEL_PROXY_DIR / "cacerts.jks"
    combined_ca = BAZEL_PROXY_DIR / "combined_ca.pem"

    if not truststore.exists():
        print("WARNING: Java truststore not found, Bazel may fail to access BCR")
        return

    local_proxy = f"http://localhost:{BAZEL_PROXY_PORT}"
    print(f"Using bazel proxy at {local_proxy} for Bazel actions")

    # Generate bazelrc for act container
    bazelrc_content = f"""\
# Auto-generated bazelrc for act container (do not commit)
# JVM proxy settings for Bazel server (BCR access, etc.)
startup --host_jvm_args=-Dhttps.proxyHost=127.0.0.1
startup --host_jvm_args=-Dhttps.proxyPort={BAZEL_PROXY_PORT}
startup --host_jvm_args=-Djavax.net.ssl.trustStore={truststore}
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit

# Propagate proxy env vars into sandbox actions
build --action_env=HTTPS_PROXY={local_proxy}
build --action_env=HTTP_PROXY={local_proxy}
build --action_env=https_proxy={local_proxy}
build --action_env=http_proxy={local_proxy}
"""

    if combined_ca.exists():
        bazelrc_content += f"""
# Node.js CA bundle for npm, puppeteer, etc.
build --action_env=NODE_EXTRA_CA_CERTS={combined_ca}
"""

    # Skip puppeteer browser downloads in lifecycle hooks
    # The browser binaries aren't needed for building - only for running tests
    # Bazel's sandbox doesn't have network access via global-agent since NODE_PATH
    # to container's global modules isn't mounted in the sandbox
    # Puppeteer 23.x has separate skip vars for each product
    bazelrc_content += """
# Skip puppeteer browser download - not needed for build, only for tests
build --action_env=PUPPETEER_SKIP_DOWNLOAD=true
build --action_env=PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
build --action_env=PUPPETEER_CHROME_SKIP_DOWNLOAD=true
build --action_env=PUPPETEER_SKIP_CHROME_HEADLESS_SHELL_DOWNLOAD=true
"""

    workspace_bazelrc.write_text(bazelrc_content)
    print(f"Generated {workspace_bazelrc}")


def main() -> int:
    """Main entry point."""
    # Parse arguments
    args = sys.argv[1:]
    job = args[0] if args else "-l"
    extra_args = args[1:] if len(args) > 1 else []

    # Set DOCKER_HOST for podman
    os.environ["DOCKER_HOST"] = f"unix://{PODMAN_SOCKET}"

    # Find act binary
    act_bin = find_act()

    # Handle list jobs command
    if job == "-l":
        return run([act_bin, "-l", *extra_args], check=False).returncode

    # Setup for running a job
    ca_bundle = ensure_ca_bundle()
    ensure_podman_running()
    cleanup_containers()
    runner_image, use_local_image = check_custom_image()

    # Generate workspace bazelrc for the container
    generate_workspace_bazelrc()

    print(f"Running job: {job}")
    print(f"CA bundle: {ca_bundle}")
    print()

    # Build and run act command
    cmd = build_act_command(act_bin, job, runner_image, use_local_image, ca_bundle, extra_args)

    return run(cmd, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
