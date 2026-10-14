#!/usr/bin/env python3
"""Run a command in a fresh network namespace with an optional Ollama-only bridge.

The evaluated process has no network interfaces/routes. A temporary, mode-0600
Unix socket is the only bridge into the host; a host-side socat listener forwards
that socket exclusively to the configured loopback inference endpoint. Inside the
namespace, a second socat process exposes that bridge on loopback for ordinary
OpenAI-compatible clients. Additional read-only mounts are available for pinned
offline runtimes. Bubblewrap and socat must be available on PATH from the Nix
environment.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--config-dir", type=Path, help="OpenCode config directory to expose read-only as ~/.config/opencode"
    )
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=11436)
    parser.add_argument("--proxy-port", type=int, default=11437)
    parser.add_argument(
        "--no-backend", action="store_true", help="run an offline command without creating an inference bridge"
    )
    parser.add_argument(
        "--readonly-bind",
        action="append",
        nargs=2,
        metavar=("HOST_PATH", "NAMESPACE_PATH"),
        type=Path,
        help="add a read-only host path at an absolute path inside the namespace",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("provide a command after --")
    args.workspace = args.workspace.resolve(strict=True)
    if not args.workspace.is_dir():
        parser.error("--workspace must name a directory")
    if args.config_dir is not None:
        args.config_dir = args.config_dir.resolve(strict=True)
        if not args.config_dir.is_dir():
            parser.error("--config-dir must name a directory")
    return args


def _offline_bootstrap(*, socat: str, proxy_port: int, backend_enabled: bool, command: list[str]) -> list[str]:
    # This code runs after entering the new network namespace. The only
    # reachable endpoint is a Unix socket that the host-side relay fixes to the
    # configured 127.0.0.1 inference service.
    bootstrap = textwrap.dedent(
        """\
        import os, shutil, socket, subprocess, sys, time, urllib.request
        if os.path.isdir("/tmp/offline-config-source"):
            shutil.copytree(
                "/tmp/offline-config-source",
                "/tmp/offline-home/.config/opencode",
                dirs_exist_ok=True,
            )
        backend_enabled = sys.argv[3] == "1"
        relay = None
        try:
            if backend_enabled:
                relay = subprocess.Popen([
                    sys.argv[1],
                    f"TCP-LISTEN:{sys.argv[2]},bind=127.0.0.1,reuseaddr,fork",
                    "UNIX-CONNECT:/tmp/offline-bridge/backend.sock",
                ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if backend_enabled:
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{sys.argv[2]}/api/tags", timeout=2
                        ):
                            break
                    except OSError:
                        if relay is not None and relay.poll() is not None:
                            raise RuntimeError("loopback relay exited before health check")
                        time.sleep(0.1)
                else:
                    raise RuntimeError("loopback backend health check did not succeed")
            try:
                with socket.create_connection(("1.1.1.1", 443), timeout=1):
                    raise RuntimeError("offline network check failed: public egress works")
            except OSError:
                pass
            if backend_enabled:
                print("offline verified: loopback backend reachable; public egress denied", file=sys.stderr)
            else:
                print("offline verified: public egress denied; no backend bridge enabled", file=sys.stderr)
            result = subprocess.run(sys.argv[4:], check=False)
            raise SystemExit(result.returncode)
        finally:
            if relay is not None:
                relay.terminate()
                try:
                    relay.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.wait()
        """
    )
    return [sys.executable, "-c", bootstrap, socat, str(proxy_port), "1" if backend_enabled else "0", *command]


def main() -> int:
    args = _parse_args()
    bwrap = shutil.which("bwrap")
    socat = shutil.which("socat")
    if bwrap is None or socat is None:
        print("bwrap and socat must be on PATH; use the repository Nix environment", file=sys.stderr)
        return 2

    bridge_dir = Path(tempfile.mkdtemp(prefix="ducktape-offline-bridge-"))
    bridge_socket = bridge_dir / "backend.sock"
    if args.no_backend:
        host_relay = None
    else:
        relay_command = [
            socat,
            f"UNIX-LISTEN:{bridge_socket},fork,unlink-early,mode=600",
            f"TCP:{args.backend_host}:{args.backend_port}",
        ]
    if not args.no_backend:
        host_relay = subprocess.Popen(
            relay_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    try:
        if host_relay is not None:
            for _ in range(50):
                if bridge_socket.exists():
                    break
                if host_relay.poll() is not None:
                    raise RuntimeError("host-side Unix socket relay exited during startup")
                time.sleep(0.1)
            else:
                raise RuntimeError("host-side Unix socket relay did not create its socket")

        command = _offline_bootstrap(
            socat=socat, proxy_port=args.proxy_port, backend_enabled=host_relay is not None, command=args.command
        )
        bwrap_args = [
            bwrap,
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/root",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/offline-home",
            "--dir",
            "/tmp/offline-home/.config",
            "--dir",
            "/tmp/offline-home/.cache",
            "--dir",
            "/tmp/offline-home/.local",
            "--dir",
            "/tmp/offline-home/.local/share",
            "--dir",
            "/tmp/offline-home/.local/state",
            "--dir",
            "/tmp/eval-work",
            "--bind",
            str(args.workspace),
            "/tmp/eval-work",
            "--clearenv",
            "--setenv",
            "PATH",
            os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
            "--setenv",
            "HOME",
            "/tmp/offline-home",
            "--setenv",
            "XDG_CONFIG_HOME",
            "/tmp/offline-home/.config",
            "--setenv",
            "XDG_CACHE_HOME",
            "/tmp/eval-work/.cache" if args.no_backend else "/tmp/offline-home/.cache",
            "--setenv",
            "XDG_DATA_HOME",
            "/tmp/offline-home/.local/share",
            "--setenv",
            "XDG_STATE_HOME",
            "/tmp/offline-home/.local/state",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "LANG",
            os.environ.get("LANG", "C.UTF-8"),
        ]
        if args.config_dir is not None:
            bwrap_args.extend(
                ["--dir", "/tmp/offline-config-source", "--ro-bind", str(args.config_dir), "/tmp/offline-config-source"]
            )
        if host_relay is not None:
            bwrap_args.extend(
                ["--dir", "/tmp/offline-bridge", "--ro-bind", str(bridge_socket), "/tmp/offline-bridge/backend.sock"]
            )
        for host_path, namespace_path in args.readonly_bind or []:
            resolved_host_path = host_path.resolve(strict=True)
            if not namespace_path.is_absolute() or namespace_path == Path("/"):
                raise ValueError(f"read-only mount target must be a non-root absolute path: {namespace_path}")
            parent_paths = list(reversed(namespace_path.parents))
            for parent in parent_paths:
                if str(parent) not in {"/", "/tmp", "/home", "/root", "/dev", "/proc"}:
                    bwrap_args.extend(["--dir", str(parent)])
            if resolved_host_path.is_dir():
                bwrap_args.extend(["--dir", str(namespace_path)])
            bwrap_args.extend(["--ro-bind", str(resolved_host_path), str(namespace_path)])
        bwrap_args.extend(["--chdir", "/tmp/eval-work", "--", *command])
        completed = subprocess.run(bwrap_args, check=False)
        return completed.returncode
    finally:
        if host_relay is not None:
            host_relay.terminate()
            try:
                host_relay.wait(timeout=2)
            except subprocess.TimeoutExpired:
                host_relay.kill()
                host_relay.wait()
        shutil.rmtree(bridge_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
