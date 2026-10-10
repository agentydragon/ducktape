"""The fixed addresses on the home LAN behind the AT&T gateway, from `home-lan.json`, which
cluster/terraform/main/home-nodes.tf also reads."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network

from pydantic import BaseModel, ConfigDict, Field

from util.bazel.runfiles import get_required_path


class HomeLan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")  # ignore _comment

    network: IPv4Network = Field(description="The gateway's LAN subnet.")
    gateway: IPv4Address = Field(description="The AT&T BGW320 gateway and its web UI.")
    switch: IPv4Address = Field(description="The MikroTik CRS310 switch's management address.")
    optiplex: IPv4Address = Field(description="optiplex's fixed address, beside its DHCP lease.")


HOME_LAN = HomeLan.model_validate_json(get_required_path("_main/home-lan.json").read_text())
