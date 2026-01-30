"""Test whether Podman works on BuildBuddy remote execution workers.

Run with: bazel test --spawn_strategy=remote --test_timeout=300 //tools:test_rbe_podman
Requires the RBE platform to use the custom rbe-worker image with podman.
"""

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import pytest_bazel


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False, **kwargs)


def test_podman_on_rbe():
    podman = shutil.which("podman")
    if not podman:
        pytest.skip("podman not installed (not using rbe-worker image?)")

    r = _run(["podman", "--version"])
    print(f"podman version: {r.stdout.strip()}")

    # Configure storage (VFS - no overlay/fuse needed)
    storage_dir = Path("/tmp/podman-test/storage")
    runroot_dir = Path("/tmp/podman-test/runroot")
    conf_dir = Path("/tmp/podman-test/conf")
    for d in [storage_dir, runroot_dir, conf_dir]:
        d.mkdir(parents=True, exist_ok=True)

    storage_conf = conf_dir / "storage.conf"
    storage_conf.write_text(
        textwrap.dedent(f"""\
        [storage]
        driver = "vfs"
        runroot = "{runroot_dir}"
        graphroot = "{storage_dir}"
    """)
    )

    containers_conf = conf_dir / "containers.conf"
    containers_conf.write_text(
        textwrap.dedent("""\
        [containers]
        userns = "host"
        annotations = ["run.oci.keep_original_groups=1"]

        [engine]
        network_backend = "cni"
    """)
    )

    # Write policy.json
    policy_dir = Path("~/.config/containers").expanduser()
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "policy.json").write_text('{"default":[{"type":"insecureAcceptAnything"}]}')

    env = {**os.environ, "CONTAINERS_STORAGE_CONF": str(storage_conf), "CONTAINERS_CONF": str(containers_conf)}

    # Try running a simple container
    print("\nTrying: podman run --rm alpine echo hello")
    r = _run(["podman", "run", "--rm", "docker.io/library/alpine:latest", "echo", "hello"], env=env)
    print(f"rc={r.returncode}")
    print(f"stdout: {r.stdout.strip()}")
    if r.returncode != 0:
        print(f"stderr: {r.stderr[:1000]}")

    # Try podman system service (Docker-compatible API socket)

    print("\nTrying: podman system service (socket mode)...")
    socket_path = Path("/tmp/podman-test.sock")
    service = subprocess.Popen(
        ["podman", "system", "service", "--time=0", f"unix://{socket_path}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    time.sleep(2)

    if socket_path.exists():
        print(f"Socket created: {socket_path}")
        r = _run(["curl", "-s", "--unix-socket", str(socket_path), "http://d/v1.43/version"])
        print(f"API /version: {r.stdout[:200]}")
    else:
        print("Socket NOT created after 2s")
        if service.poll() is not None:
            _, stderr = service.communicate(timeout=5)
            print(f"Service exited: rc={service.returncode}")
            print(f"stderr: {stderr.decode()[:500]}")

    service.terminate()
    service.wait(timeout=5)

    # Probe only — don't fail on container issues, just log results


if __name__ == "__main__":
    pytest_bazel.main()
