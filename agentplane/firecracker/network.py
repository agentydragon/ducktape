"""Pod-local TAP and packet fence; only the relay and declared routes face the guest."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

GATEWAY_V4 = "192.0.2.1"
GUEST_V4 = "192.0.2.2"
GATEWAY_V6 = "fd00:fc::1"
TAP = "tap0"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def exists(*args: str) -> bool:
    return subprocess.run(args, check=False, capture_output=True).returncode == 0


@dataclass
class Network:
    relay_port: int
    platform_route_ports: list[int]

    @staticmethod
    def guards(tool: str) -> list[tuple[str, ...]]:
        return [
            (tool, "INPUT", "-i", TAP, "-j", "REJECT"),
            (tool, "FORWARD", "-i", TAP, "-j", "REJECT"),
            (tool, "FORWARD", "-o", TAP, "-j", "REJECT"),
        ]

    def install_guards(self) -> None:
        for tool in ("iptables", "ip6tables"):
            for _, parent, *rule in self.guards(tool):
                run(tool, "-I", parent, "1", *rule)

    def remove_guards(self) -> None:
        for tool in ("iptables", "ip6tables"):
            for _, parent, *rule in self.guards(tool):
                while exists(tool, "-C", parent, *rule):
                    run(tool, "-D", parent, *rule)

    def setup(self) -> None:
        self.install_guards()
        if exists("ip", "link", "show", TAP):
            run("ip", "link", "del", TAP)
        run("ip", "tuntap", "add", TAP, "mode", "tap")
        run("ip", "addr", "add", f"{GATEWAY_V4}/30", "dev", TAP)
        run("ip", "-6", "addr", "add", f"{GATEWAY_V6}/64", "dev", TAP, "nodad")
        run("ip", "link", "set", TAP, "mtu", "1280", "up")
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
        for tool in ("iptables", "ip6tables"):
            for chain in ("AP_TAP_INPUT", "AP_TAP_FORWARD", "AP_TAP_INGRESS"):
                if not exists(tool, "-S", chain):
                    run(tool, "-N", chain)
            for chain, direction, destination in (
                ("AP_TAP_INPUT", "-i", "INPUT"),
                ("AP_TAP_FORWARD", "-i", "FORWARD"),
                ("AP_TAP_INGRESS", "-o", "FORWARD"),
            ):
                rule = (direction, TAP, "-j", chain)
                if not exists(tool, "-C", destination, *rule):
                    run(tool, "-A", destination, *rule)
        nat_rule = ("-p", "tcp", "--dport", "9090", "-j", "DNAT", "--to-destination", f"{GUEST_V4}:9090")
        if not exists("iptables", "-t", "nat", "-C", "PREROUTING", *nat_rule):
            run("iptables", "-t", "nat", "-A", "PREROUTING", *nat_rule)
        self.disable()

    def disable(self) -> None:
        self.install_guards()
        for tool in ("iptables", "ip6tables"):
            run(tool, "-F", "AP_TAP_INPUT")
            run(tool, "-F", "AP_TAP_FORWARD")
            run(tool, "-F", "AP_TAP_INGRESS")
            if tool == "ip6tables":
                for kind in ("neighbor-solicitation", "neighbor-advertisement"):
                    run(
                        tool,
                        "-A",
                        "AP_TAP_INPUT",
                        "-p",
                        "ipv6-icmp",
                        "--icmpv6-type",
                        kind,
                        "-m",
                        "hl",
                        "--hl-eq",
                        "255",
                        "-j",
                        "ACCEPT",
                    )
            run(tool, "-A", "AP_TAP_INPUT", "-j", "REJECT")
            run(tool, "-A", "AP_TAP_FORWARD", "-j", "REJECT")
            run(tool, "-A", "AP_TAP_INGRESS", "-j", "REJECT")
        self.remove_guards()

    def enable(self) -> None:
        self.install_guards()
        for tool in ("iptables", "ip6tables"):
            run(tool, "-F", "AP_TAP_INPUT")
            run(tool, "-F", "AP_TAP_FORWARD")
            run(tool, "-F", "AP_TAP_INGRESS")
            if tool == "ip6tables":
                for kind in ("neighbor-solicitation", "neighbor-advertisement"):
                    run(
                        tool,
                        "-A",
                        "AP_TAP_INPUT",
                        "-p",
                        "ipv6-icmp",
                        "--icmpv6-type",
                        kind,
                        "-m",
                        "hl",
                        "--hl-eq",
                        "255",
                        "-j",
                        "ACCEPT",
                    )
            run(tool, "-A", "AP_TAP_INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
            for port in [self.relay_port, *self.platform_route_ports]:
                run(tool, "-A", "AP_TAP_INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT")
            run(tool, "-A", "AP_TAP_INPUT", "-j", "REJECT")
            run(tool, "-A", "AP_TAP_FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
            run(tool, "-A", "AP_TAP_FORWARD", "-j", "REJECT")
            if tool == "iptables":
                run(tool, "-A", "AP_TAP_INGRESS", "-d", GUEST_V4, "-p", "tcp", "--dport", "9090", "-j", "ACCEPT")
            run(tool, "-A", "AP_TAP_INGRESS", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
            run(tool, "-A", "AP_TAP_INGRESS", "-j", "REJECT")
        self.remove_guards()
