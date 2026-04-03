"""HTTP clients for Firecracker API and process_api control server.

Each client wraps an httpx.Client targeting a specific pod IP + port.
The manager creates these after a pod reaches Running state.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_fixed

logger = logging.getLogger(__name__)

FIRECRACKER_API_PORT = 2026
PROCESS_API_CONTROL_PORT = 2025
PROCESS_API_WS_PORT = 2024

_TIMEOUT = 10.0
_HEALTH_TIMEOUT = 2.0


class _NotReadyError(Exception):
    pass


class FirecrackerClient:
    """HTTP client for the Firecracker management API (proxied on :2026)."""

    def __init__(self, pod_ip: str, port: int = FIRECRACKER_API_PORT) -> None:
        self._base = f"http://{pod_ip}:{port}"
        self._client = httpx.Client(timeout=_TIMEOUT)

    def close(self) -> None:
        self._client.close()

    def put(self, path: str, body: dict) -> None:
        self._client.put(f"{self._base}{path}", json=body).raise_for_status()

    def patch(self, path: str, body: dict) -> None:
        self._client.patch(f"{self._base}{path}", json=body).raise_for_status()

    def boot(
        self,
        *,
        kernel_path: str,
        rootfs_path: str,
        initramfs_path: str | None = None,
        vcpus: int = 2,
        mem_mib: int = 4096,
        boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off",
        guest_mac: str = "AA:FC:00:00:00:01",
        tap_name: str = "tap0",
    ) -> None:
        """Configure and start a fresh VM."""
        boot_source: dict[str, str] = {"kernel_image_path": kernel_path, "boot_args": boot_args}
        if initramfs_path:
            boot_source["initrd_path"] = initramfs_path
        self.put("/boot-source", boot_source)

        self.put(
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": rootfs_path,
                "is_root_device": initramfs_path is None,
                "is_read_only": False,
            },
        )
        self.put("/machine-config", {"vcpu_count": vcpus, "mem_size_mib": mem_mib})
        self.put("/network-interfaces/eth0", {"iface_id": "eth0", "host_dev_name": tap_name, "guest_mac": guest_mac})
        self.put("/actions", {"action_type": "InstanceStart"})
        logger.info("Firecracker VM started via API on %s", self._base)

    def pause(self) -> None:
        self.put("/vm", {"state": "Paused"})

    def resume(self) -> None:
        self.put("/vm", {"state": "Resumed"})

    def create_snapshot(self, *, snapshot_path: str, mem_file_path: str) -> None:
        self.put(
            "/snapshot/create",
            {"snapshot_type": "Full", "snapshot_path": snapshot_path, "mem_file_path": mem_file_path},
        )

    def load_snapshot(self, *, snapshot_path: str, mem_file_path: str, resume: bool = True) -> None:
        self.put(
            "/snapshot/load",
            {
                "snapshot_path": snapshot_path,
                "mem_backend": {"backend_type": "File", "backend_path": mem_file_path},
                "enable_diff_snapshots": False,
                "resume_vm": resume,
            },
        )

    def wait_ready(self, timeout_secs: int = 30) -> None:
        """Wait for the Firecracker API proxy to accept connections."""

        @retry(
            retry=retry_if_exception_type(_NotReadyError),
            stop=stop_after_delay(timeout_secs),
            wait=wait_fixed(1),
            reraise=True,
        )
        def _poll() -> None:
            try:
                resp = self._client.get(f"{self._base}/", timeout=_HEALTH_TIMEOUT)
                if resp.status_code >= 500:
                    raise _NotReadyError(f"Firecracker API returned {resp.status_code}")
            except httpx.HTTPError as e:
                raise _NotReadyError(str(e)) from e

        try:
            _poll()
        except _NotReadyError:
            raise TimeoutError(f"Firecracker API proxy not ready at {self._base} after {timeout_secs}s") from None


class ProcessApiControl:
    """HTTP client for process_api's control server (proxied on :2025)."""

    def __init__(self, pod_ip: str, port: int = PROCESS_API_CONTROL_PORT) -> None:
        self._base = f"http://{pod_ip}:{port}"
        self._client = httpx.Client(timeout=_TIMEOUT)

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            resp = self._client.get(f"{self._base}/health", timeout=_HEALTH_TIMEOUT)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def freeze_filesystem(self) -> None:
        self._client.post(f"{self._base}/fs_freeze").raise_for_status()

    def thaw_filesystem(self) -> None:
        self._client.post(f"{self._base}/fs_thaw").raise_for_status()

    def shutdown(self) -> None:
        self._client.post(f"{self._base}/shutdown")

    def wait_ready(self, timeout_secs: int = 60) -> None:
        """Wait for /health to respond."""

        @retry(
            retry=retry_if_exception_type(_NotReadyError),
            stop=stop_after_delay(timeout_secs),
            wait=wait_fixed(1),
            reraise=True,
        )
        def _poll() -> None:
            if not self.health():
                raise _NotReadyError("not healthy")

        try:
            _poll()
        except _NotReadyError:
            raise TimeoutError(f"process_api not ready at {self._base} after {timeout_secs}s") from None
