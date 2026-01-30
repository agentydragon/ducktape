"""Probe BuildBuddy remote execution environment."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest_bazel


def test_probe(capsys):
    print(f"\nuid={os.getuid()} euid={os.geteuid()}")
    uname = os.uname()
    print(f"kernel={uname.release} machine={uname.machine}")

    for path_str in [
        "/proc/self/uid_map",
        "/proc/self/setgroups",
        "/proc/sys/kernel/unprivileged_userns_clone",
        "/dev/fuse",
    ]:
        p = Path(path_str)
        extra = ""
        if p.exists() and p.is_file():
            try:
                extra = f" = {p.read_text().strip()[:80]!r}"
            except Exception as e:
                extra = f" read_err={e}"
        print(f"{path_str}: exists={p.exists()}{extra}")

    for ns_flag in ["--user", "--mount"]:
        try:
            r = subprocess.run(["unshare", ns_flag, "true"], check=False, capture_output=True, timeout=5)
            s = "OK" if r.returncode == 0 else f"FAIL({r.returncode}): {r.stderr.decode().strip()}"
        except FileNotFoundError:
            s = "NOT FOUND"
        except Exception as e:
            s = str(e)
        print(f"unshare {ns_flag}: {s}")

    for tool in ["newuidmap", "newgidmap", "podman", "docker", "apt-get", "crun", "runc"]:
        print(f"{tool}: {shutil.which(tool) or 'NOT FOUND'}")

    try:
        r = subprocess.run(["stat", "-f", "-c", "%T", "/"], check=False, capture_output=True, timeout=5)
        print(f"root fs: {r.stdout.decode().strip()}")
    except Exception:
        pass

    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if "Seccomp" in line or "Cap" in line:
                print(f"  {line.strip()}")


if __name__ == "__main__":
    pytest_bazel.main()
