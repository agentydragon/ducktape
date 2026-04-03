"""TAP device and NAT setup for Firecracker VM networking.

Creates a TAP device bridged to the pod's network, with NAT (MASQUERADE)
so the guest VM can reach the internet through the pod's eth0.

Uses pyroute2 (netlink) for TAP/IP and nft CLI for NAT. The only system
binary needed is ``nft`` from the nftables package — all other networking
(TAP creation, IP assignment, link up, sysctl) is pure Python.

Network layout:
  Pod eth0 (CNI-assigned IP)
    └─ nftables MASQUERADE
  tap0 (10.0.0.1/30)
    └─ Firecracker VM eth0 (10.0.0.2/30, gw 10.0.0.1)
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyroute2 import IPRoute

logger = logging.getLogger(__name__)

# Must match process_api's hardcoded networking (firecracker_init.rs):
# guest IP 192.0.2.2/24, gateway 192.0.2.1, MTU 1400.
TAP_NAME = "tap0"
TAP_IP = "192.0.2.1"
GUEST_IP = "192.0.2.2"
SUBNET = "192.0.2.0/24"
PREFIX_LEN = 24
HOST_IFACE = "eth0"

_NFT_TABLE = "fc-nat"


@dataclass(frozen=True)
class NetworkSetup:
    """Result of TAP + NAT setup."""

    tap_name: str
    tap_ip: str
    guest_ip: str
    guest_gateway: str
    guest_netmask: str


def _enable_ip_forward() -> None:
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
    logger.debug("Enabled ip_forward")


def _create_tap(ipr: IPRoute) -> None:
    """Create TAP device, assign IP, bring up via netlink."""
    ipr.link("add", ifname=TAP_NAME, kind="tuntap", tuntap_mode="tap")
    idx = ipr.link_lookup(ifname=TAP_NAME)[0]
    ipr.addr("add", index=idx, address=TAP_IP, prefixlen=PREFIX_LEN)
    ipr.link("set", index=idx, state="up")
    logger.debug("Created TAP %s (%s/%d)", TAP_NAME, TAP_IP, PREFIX_LEN)


def _setup_nat() -> None:
    """Create nftables MASQUERADE rule for guest → internet NAT.

    Uses the nft CLI — the only system binary this module needs.
    The pyroute2 NFTables class exposes raw netlink but doesn't have a
    high-level API for building nft expressions, so the CLI is simpler.
    """
    # Single atomic nft command that creates table + chain + rule.
    ruleset = f"""
table ip {_NFT_TABLE} {{
    chain postrouting {{
        type nat hook postrouting priority 100; policy accept;
        ip saddr {SUBNET} oifname "{HOST_IFACE}" masquerade
    }}
}}
"""
    subprocess.run(["nft", "-f", "-"], input=ruleset, check=True, text=True, capture_output=True)
    logger.debug("NAT: %s via %s → MASQUERADE", SUBNET, HOST_IFACE)


def _teardown_nat() -> None:
    subprocess.run(["nft", "delete", "table", "ip", _NFT_TABLE], check=False, capture_output=True, text=True)


def setup_tap_and_nat() -> NetworkSetup:
    """Create TAP device, assign IP, enable forwarding, set up NAT."""
    _enable_ip_forward()
    with IPRoute() as ipr:
        _create_tap(ipr)
    _setup_nat()

    logger.info("Network ready: TAP=%s (%s), guest=%s, NAT via %s", TAP_NAME, TAP_IP, GUEST_IP, HOST_IFACE)
    return NetworkSetup(
        tap_name=TAP_NAME, tap_ip=TAP_IP, guest_ip=GUEST_IP, guest_gateway=TAP_IP, guest_netmask="255.255.255.0"
    )


def teardown_tap_and_nat() -> None:
    """Remove TAP device and NAT rules. Best-effort, logs errors."""
    _teardown_nat()
    try:
        with IPRoute() as ipr:
            links = ipr.link_lookup(ifname=TAP_NAME)
            if links:
                ipr.link("del", index=links[0])
    except Exception:
        logger.warning("Failed to remove TAP %s", TAP_NAME, exc_info=True)
