"""The fixed addresses on the home LAN behind the AT&T gateway (docs/home_lan.md).

cluster/terraform/main/home-nodes.tf and tf/gitops/home-switch/bootstrap.sh repeat the ones
they need; keep them in sync.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network

from pydantic import BaseModel, ConfigDict, Field


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
