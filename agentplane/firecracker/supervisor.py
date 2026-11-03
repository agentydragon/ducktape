"""Single owner of a Pod's Firecracker continuation and RAM checkpoint."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from agentplane.firecracker.config import Config, load
from agentplane.firecracker.firecracker_api import FirecrackerApi, process
from agentplane.firecracker.guest_management import sync_guest
from agentplane.firecracker.network import GUEST_V4, Network
from agentplane.firecracker.storage import WORK, Manifest, Storage, owner_lock

LOG = logging.getLogger(__name__)
INTENT = Path("/run/agentplane/firecracker-intent/running")
FIRECRACKER = Path("/usr/local/bin/firecracker")
API_SOCKET = Path("/work/firecracker.sock")
VSOCK_SOCKET = Path("/work/vsock")


class Phase(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    FAILED = "failed"


def wait_for(predicate: Callable[[], bool], stopped: threading.Event, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if stopped.is_set():
            raise RuntimeError("launcher is stopping")
        if time.monotonic() >= deadline:
            raise TimeoutError("guest or VMM did not become ready")
        stopped.wait(min(0.1, deadline - time.monotonic()))


def read_intent(path: Path) -> bool:
    value = path.read_text().strip()
    if value not in {"true", "false"}:
        raise ValueError("Firecracker intent must be exactly true or false")
    return value == "true"


def guest_reachable() -> bool:
    try:
        with socket.create_connection((GUEST_V4, 9090), timeout=0.5):
            return True
    except OSError:
        return False


class StatusHandler(BaseHTTPRequestHandler):
    supervisor: ClassVar[Supervisor]

    def log_message(self, format: str, *args: object) -> None:
        LOG.info("status HTTP: " + format, *args)

    def do_GET(self) -> None:
        if self.path == "/state":
            status: dict[str, object] = {"pod_uid": self.supervisor.config.pod_uid, "phase": self.supervisor.phase}
            if self.supervisor.error is not None:
                status["error"] = self.supervisor.error
            self.respond(200, status)
        elif self.path == "/readyz":
            self.respond(200 if self.supervisor.phase == Phase.RUNNING and guest_reachable() else 503, {})
        else:
            self.respond(404, {})

    def respond(self, code: int, value: dict[str, object]) -> None:
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Supervisor:
    def __init__(self, config: Config, storage: Storage, network: Network, intent_path: Path = INTENT) -> None:
        self.config = config
        self.storage = storage
        self.network = network
        self.intent_path = intent_path
        self.phase = Phase.STARTING
        self.error: str | None = None
        self.stopped = threading.Event()

    def configure(self, api: FirecrackerApi) -> None:
        paths = self.storage.paths()
        api.request(
            "PUT", "/machine-config", {"vcpu_count": self.config.vcpu_count, "mem_size_mib": self.config.memory_mib}
        )
        init = (self.config.boot_directory / "init").read_text().strip()
        if not init.startswith("/") or " " in init:
            raise ValueError("boot init must be one absolute guest path")
        api.request(
            "PUT",
            "/boot-source",
            {
                "kernel_image_path": str(paths["kernel"]),
                "boot_args": f"console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rootwait rootfstype=ext4 rw init={init}",
            },
        )
        for index, (name, readonly) in enumerate(
            (
                ("rootfs", False),
                ("runner", True),
                ("state", False),
                ("workspace", False),
                ("config", True),
                ("launch", True),
                ("trust", True),
                ("credentials", True),
            )
        ):
            api.request(
                "PUT",
                f"/drives/{name}",
                {
                    "drive_id": name,
                    "path_on_host": str(paths[name]),
                    "is_root_device": index == 0,
                    "is_read_only": readonly,
                },
            )
        api.request(
            "PUT",
            "/network-interfaces/eth0",
            {"iface_id": "eth0", "host_dev_name": "tap0", "guest_mac": "06:00:ac:10:00:02"},
        )
        api.request("PUT", "/vsock", {"guest_cid": 3, "uds_path": str(VSOCK_SOCKET)})
        api.request("PUT", "/actions", {"action_type": "InstanceStart"})

    def serve_status(self) -> None:
        StatusHandler.supervisor = self
        with ThreadingHTTPServer(("0.0.0.0", 9092), StatusHandler) as server:
            server.timeout = 0.5
            while not self.stopped.is_set():
                server.handle_request()

    def run(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        Path("/work").mkdir(parents=True, exist_ok=True)
        server = threading.Thread(target=self.serve_status, daemon=True)
        server.start()
        network_ready = False
        try:
            with owner_lock():
                if self.storage.active_path.exists():
                    raise RuntimeError("prior VM continuation has an uncertain outcome")
                self.storage.read_manifest()
                self.network.setup()
                network_ready = True
                self.reconcile()
        except Exception as error:
            self.error = str(error)
            self.phase = Phase.FAILED
            LOG.exception("Firecracker launcher failed closed")
            if network_ready:
                try:
                    self.network.disable()
                except Exception:
                    LOG.exception("Firecracker network fence failed while handling launcher failure")
            self.stopped.wait()
        finally:
            self.stopped.set()
            server.join(timeout=2)

    def reconcile(self) -> None:
        while not self.stopped.is_set():
            if not read_intent(self.intent_path):
                self.phase = Phase.SUSPENDED
                self.stopped.wait(0.2)
                continue
            manifest = self.storage.read_manifest()
            if manifest is None:
                self.storage.prepare()
            self.run_continuation(manifest)

    def run_continuation(self, manifest: Manifest | None) -> None:
        self.phase = Phase.RESUMING if manifest else Phase.STARTING
        self.storage.mark_active()
        API_SOCKET.unlink(missing_ok=True)
        VSOCK_SOCKET.unlink(missing_ok=True)
        with process(FIRECRACKER, API_SOCKET, WORK / "firecracker.log") as child:
            wait_for(lambda: API_SOCKET.exists() or child.poll() is not None, self.stopped, 15)
            if child.poll() is not None:
                raise RuntimeError("Firecracker exited before its API became ready")
            api = FirecrackerApi(API_SOCKET)
            if manifest is None:
                self.configure(api)
            else:
                self.storage.restore(manifest)
                directory = WORK / manifest.generation
                api.request(
                    "PUT",
                    "/snapshot/load",
                    {
                        "snapshot_path": str(directory / "vmstate"),
                        "mem_file_path": str(directory / "memory"),
                        "resume_vm": True,
                    },
                )
            boot_id = sync_guest(VSOCK_SOCKET, self.stopped, manifest.boot_id if manifest else "")
            if manifest is not None and boot_id != manifest.boot_id:
                raise RuntimeError("restored guest boot ID differs from checkpoint")
            LOG.info("guest thaw acknowledged boot ID %s", boot_id)
            self.network.enable()
            wait_for(guest_reachable, self.stopped, 30)
            self.phase = Phase.RUNNING
            while not self.stopped.is_set() and read_intent(self.intent_path):
                if child.poll() is not None:
                    raise RuntimeError("Firecracker exited while running")
                self.stopped.wait(0.2)
            if self.stopped.is_set():
                return
            self.phase = Phase.SUSPENDING
            self.network.disable()
            api.request("PATCH", "/vm", {"state": "Paused"})
            scratch = WORK / f".capture-{os.getpid()}"
            scratch.mkdir(exist_ok=False)
            api.request(
                "PUT",
                "/snapshot/create",
                {
                    "snapshot_type": "Full",
                    "snapshot_path": str(scratch / "vmstate"),
                    "mem_file_path": str(scratch / "memory"),
                },
            )
            checkpoint = self.storage.checkpoint(scratch / "memory", scratch / "vmstate", boot_id)
        # Releasing the marker after the VMM has exited makes crash recovery conservative.
        self.storage.finish_checkpoint(checkpoint, scratch)
        self.storage.clear_active()
        self.phase = Phase.SUSPENDED


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/run/agentplane/firecracker/config.json"))
    args = parser.parse_args()
    config = load(args.config)
    supervisor = Supervisor(
        config=config,
        storage=Storage(config, FIRECRACKER),
        network=Network(config.relay_port, config.platform_route_ports),
    )
    signal.signal(signal.SIGTERM, lambda _signal, _frame: supervisor.stopped.set())
    signal.signal(signal.SIGINT, lambda _signal, _frame: supervisor.stopped.set())
    supervisor.run()


if __name__ == "__main__":
    main()
