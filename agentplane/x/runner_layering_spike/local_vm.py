"""Boot the minimal layering initramfs locally under the repo-pinned Firecracker VMM."""

from __future__ import annotations

import argparse
import queue
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import TextIO

import httpx

from util.bazel.runfiles import get_required_path, own_repo_rlocation


def _put(client: httpx.Client, path: str, body: dict[str, object], output: list[str]) -> None:
    response = client.put(path, json=body)
    if response.is_error:
        raise RuntimeError(
            f"Firecracker {path} returned HTTP {response.status_code}: {response.text}\n"
            f"VMM console:\n{chr(10).join(output)}"
        )


def _read_console(stream: TextIO, output: list[str], lines: queue.Queue[str]) -> None:
    for raw_line in stream:
        line = raw_line.rstrip("\n")
        output.append(line)
        lines.put(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    args = parser.parse_args()
    kernel = args.kernel or get_required_path("firecracker_vmlinux_6_1_128/file/vmlinux-6.1.128")
    if not kernel.is_file():
        raise SystemExit(f"kernel image is not available: {kernel}")

    initramfs = get_required_path(own_repo_rlocation("agentplane/x/runner_layering_spike/local-vm-initramfs.cpio"))
    firecracker_layer = get_required_path(own_repo_rlocation("devinfra/firecracker/firecracker_layer.tar"))

    with tempfile.TemporaryDirectory(prefix="agentplane-runner-layering-") as directory:
        root = Path(directory)
        firecracker = root / "firecracker"
        with tarfile.open(firecracker_layer) as layer:
            member = layer.getmember("usr/local/bin/firecracker-v1.12.1-x86_64")
            source = layer.extractfile(member)
            assert source is not None
            with source, firecracker.open("wb") as target:
                target.write(source.read())
        firecracker.chmod(0o755)

        api_socket = root / "firecracker.sock"
        process = subprocess.Popen(
            [str(firecracker), "--api-sock", str(api_socket), "--boot-timer"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        output: list[str] = []
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=_read_console, args=(process.stdout, output, lines), daemon=True)
        reader.start()
        client: httpx.Client | None = None
        try:
            deadline = time.monotonic() + args.timeout_seconds
            while not api_socket.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if process.poll() is not None:
                raise RuntimeError("Firecracker exited before opening its API socket: " + "\n".join(output))
            if not api_socket.exists():
                raise TimeoutError("Firecracker API socket did not become ready")

            client = httpx.Client(
                base_url="http://localhost", transport=httpx.HTTPTransport(uds=str(api_socket)), timeout=5
            )
            _put(
                client,
                "/boot-source",
                {
                    "kernel_image_path": str(kernel.resolve()),
                    "initrd_path": str(initramfs),
                    "boot_args": "console=ttyS0 panic=1 init=/init",
                },
                output,
            )
            _put(client, "/machine-config", {"vcpu_count": 1, "mem_size_mib": 1024}, output)
            _put(client, "/actions", {"action_type": "InstanceStart"}, output)

            expected = ("runner-layering: child exited; parent init still owns control", "codex-cli 0.157.0")
            seen: set[str] = set()
            while time.monotonic() < deadline and seen != set(expected):
                if process.poll() is not None:
                    break
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                for marker in expected:
                    if marker in line:
                        seen.add(marker)
                if "runner-layering init:" in line:
                    raise RuntimeError("guest init failed; console:\n" + "\n".join(output))
            if seen != set(expected):
                raise TimeoutError("guest did not produce expected markers; console:\n" + "\n".join(output))
            if process.poll() is not None:
                raise RuntimeError("Firecracker exited after the child completed: " + "\n".join(output))
            print("Firecracker booted the minimal initramfs; pinned Codex exited and the parent remained alive.")
            print("Guest console markers:")
            for line in output:
                if any(marker in line for marker in expected):
                    print(line)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if client is not None:
                client.close()


if __name__ == "__main__":
    main()
