"""Entrypoint for Firecracker VM pods — infrastructure only.

The pod is deliberately dumb. It:
1. Sets up TAP + NAT networking
2. Starts the Firecracker process (no VM config — just the process + API)
3. Proxies guest and FC API ports onto the pod network
4. Blocks until Firecracker exits

All VM configuration (boot-source, drives, machine-config, InstanceStart,
snapshot/load) is done by the manager through the proxied ports.
The rootfs PVC is created and mounted by the manager before the pod starts.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from devinfra.firecracker.vm_pod.config import load_config
from devinfra.firecracker.vm_pod.firecracker_process import start_firecracker
from devinfra.firecracker.vm_pod.networking import GUEST_IP, setup_tap_and_nat
from devinfra.firecracker.vm_pod.proxy import start_tcp_to_tcp_proxy, start_tcp_to_unix_proxy

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/fc-vm-pod/config.yaml")
    logger.info("Loading config from %s", config_path)
    cfg = load_config(config_path)

    setup_tap_and_nat()
    fc = start_firecracker(cfg.firecracker_bin)

    # Proxy all services onto the pod network so the manager can reach them.
    start_tcp_to_tcp_proxy(2024, GUEST_IP, 2024)  # process_api WebSocket
    start_tcp_to_tcp_proxy(2025, GUEST_IP, 2025)  # process_api HTTP control
    start_tcp_to_unix_proxy(fc.api_socket, 2026)  # Firecracker API

    logger.info("Pod ready: WS :2024, control :2025, FC API :2026")

    def _shutdown(signum, _frame):
        logger.info("Received signal %d, shutting down...", signum)
        fc.process.terminate()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    rc = fc.process.wait()
    logger.info("Firecracker exited with code %d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
