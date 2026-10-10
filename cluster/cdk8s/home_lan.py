"""The fixed addresses on the home LAN behind the AT&T gateway (docs/home_lan.md).

`write_manifests` renders them to `cluster/generated/home-lan.json` for consumers outside
cdk8s: cluster/terraform/main/home-nodes.tf and tf/gitops/home-switch/bootstrap.sh.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cluster.cdk8s.manifest_roots import GENERATED_ROOT


class HomeLan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    network: IPv4Network = Field(description="The gateway's LAN subnet.")
    gateway: IPv4Address = Field(description="The AT&T BGW320 gateway and its web UI.")
    switch: IPv4Address = Field(description="The MikroTik CRS310 switch's management address.")
    optiplex: IPv4Address = Field(description="optiplex's fixed address, beside its DHCP lease.")


HOME_LAN = HomeLan(
    network=IPv4Network("192.168.1.0/24"),
    gateway=IPv4Address("192.168.1.254"),
    switch=IPv4Address("192.168.1.100"),
    optiplex=IPv4Address("192.168.1.10"),
)


def write_manifests(root: Path) -> None:
    (root / GENERATED_ROOT / "home-lan.json").write_text(HOME_LAN.model_dump_json(indent=2) + "\n")
