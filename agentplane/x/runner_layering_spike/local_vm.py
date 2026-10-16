"""Boot the minimal layering initramfs locally under the repo-pinned Firecracker VMM."""

from __future__ import annotations

import argparse
import json
import queue
import shutil
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


def _extract_oci_rootfs(rloc_file: str, destination: Path) -> None:
    """Expand the layers from a Bazel OCI layout into a temporary filesystem tree."""
    reference = get_required_path(own_repo_rlocation(rloc_file))
    layout = get_required_path(reference.read_text().strip())
    index = json.loads((layout / "index.json").read_text())
    (descriptor,) = index["manifests"]
    manifest_path = layout / "blobs" / descriptor["digest"].replace(":", "/")
    manifest = json.loads(manifest_path.read_text())
    for layer in manifest["layers"]:
        layer_path = layout / "blobs" / layer["digest"].replace(":", "/")
        with tarfile.open(layer_path, "r:*") as archive:
            archive.extractall(destination, filter="data")


def _make_squashfs(source: Path, destination: Path) -> None:
    executable = shutil.which("mksquashfs")
    if executable is None:
        raise RuntimeError("mksquashfs is missing; load the repository Nix devshell before running this probe")
    result = subprocess.run(
        [
            executable,
            str(source),
            str(destination),
            "-noappend",
            "-no-progress",
            "-all-root",
            "-reproducible",
            "-comp",
            "xz",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"mksquashfs failed: {result.stderr[-4000:]}\n{result.stdout[-1000:]}")


def _probe_claude_on_host(claude_binary: Path) -> None:
    """Compare the pinned Claude executable outside the VM's child chroot."""
    result = subprocess.run([str(claude_binary), "--version"], check=False, capture_output=True, text=True, timeout=30)
    stdout = result.stdout.strip()[-1000:]
    stderr = result.stderr.strip()[-1000:]
    print(f"Host Claude --version exit status: {result.returncode}")
    if stdout:
        print(f"Host Claude stdout: {stdout}")
    if stderr:
        print(f"Host Claude stderr: {stderr}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    args = parser.parse_args()
    kernel = args.kernel or get_required_path("firecracker_vmlinux_6_1_128/file/vmlinux-6.1.128")
    if not kernel.is_file():
        raise SystemExit(f"kernel image is not available: {kernel}")

    initramfs = get_required_path(own_repo_rlocation("agentplane/x/runner_layering_spike/base-initramfs.cpio"))
    firecracker_layer = get_required_path(own_repo_rlocation("devinfra/firecracker/firecracker_layer.tar"))

    with tempfile.TemporaryDirectory(prefix="agentplane-runner-layering-") as directory:
        root = Path(directory)
        tools_root = root / "tools-root"
        harness_root = root / "harness-root"
        tools_root.mkdir()
        harness_root.mkdir()
        _extract_oci_rootfs("agentplane/x/runner_layering_spike/tools_rootfs.rloc", tools_root)
        for relative in ("opt", "home/agent/.codex", "home/agent/.claude", "workspace", "tmp", "proc"):
            (tools_root / relative).mkdir(parents=True, exist_ok=True)

        harness_files = (
            ("agentplane_codex_cli_linux_x64/bin/codex", "bin/codex"),
            ("agentplane_codex_cli_linux_x64/codex-resources/bwrap", "codex-resources/bwrap"),
            ("claude_code_cli_linux_x64/claude", "claude"),
        )
        for runfile, relative in harness_files:
            destination = harness_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(get_required_path(runfile), destination)
            destination.chmod(0o555)
            if runfile == "claude_code_cli_linux_x64/claude":
                _probe_claude_on_host(destination)

        tools_image = root / "tools.squashfs"
        harness_image = root / "harness.squashfs"
        _make_squashfs(tools_root, tools_image)
        _make_squashfs(harness_root, harness_image)
        print(f"Tools SquashFS: {tools_image.stat().st_size:,} bytes")
        print(f"Harness SquashFS: {harness_image.stat().st_size:,} bytes")

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
            for drive_id, image in (("tools", tools_image), ("harness", harness_image)):
                _put(
                    client,
                    f"/drives/{drive_id}",
                    {
                        "drive_id": drive_id,
                        "path_on_host": str(image.resolve()),
                        "is_root_device": False,
                        "is_read_only": True,
                    },
                    output,
                )
            _put(client, "/actions", {"action_type": "InstanceStart"}, output)

            expected = (
                "runner-layering: mounted separate read-only tools and harness filesystems",
                "LAYERED_BASH_OK",
                "runner-layering: pinned Codex exited successfully",
                "runner-layering: Claude probe completed",
                "runner-layering: Claude root chroot comparison completed",
                "runner-layering: child exited; parent init still owns control",
            )
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
