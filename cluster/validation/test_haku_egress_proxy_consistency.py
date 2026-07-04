"""SSOT check: the haku-egress-proxy egress allowlist is maintained in two places that MUST agree.

- `squid.conf` — the L7 allowlist Squid enforces (`acl allowed dstdomain <host>` →
  `http_access allow allowed` / `deny all`): which hosts a bumped request may reach.
- `cnp-haku-cloud-api-egress.yaml` — the Squid pod's own Cilium egress (`toFQDNs: matchName`):
  which hosts the pod is allowed to connect out to at L3/L4.

If they drift, failures are silent-ish: a host allowed at L7 but missing from the CNP dies at
connect; a host in the CNP but not the L7 allowlist is a dead rule. This test pins them to one
source of truth — the exact FQDN set must be identical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_HAKU_EGRESS_PROXY = "_main/cluster/k8s/agents/haku-egress-proxy"


def _egress_proxy_allowed_hosts(conf: str) -> set[str]:
    """Every host in Squid's `acl allowed dstdomain …` lines (comments stripped, multi-host ok)."""
    hosts: set[str] = set()
    for line in conf.splitlines():
        m = re.match(r"acl\s+allowed\s+dstdomain\s+(.+)$", line.split("#", 1)[0].strip())
        if m:
            hosts.update(m.group(1).split())
    return hosts


def _cnp_egress_hosts(cnp: dict) -> set[str]:
    """Every `toFQDNs: - matchName:` host across the CNP's egress rules."""
    hosts: set[str] = set()
    for rule in cnp["spec"]["egress"]:
        for fqdn in rule.get("toFQDNs", []):
            if "matchName" in fqdn:
                hosts.add(fqdn["matchName"])
    return hosts


def test_egress_proxy_acl_matches_egress_cnp() -> None:
    conf = Path(get_required_path(f"{_HAKU_EGRESS_PROXY}/squid.conf")).read_text()
    cnp = yaml.safe_load(Path(get_required_path(f"{_HAKU_EGRESS_PROXY}/cnp-haku-cloud-api-egress.yaml")).read_text())

    acl_hosts = _egress_proxy_allowed_hosts(conf)
    egress_hosts = _cnp_egress_hosts(cnp)

    only_in_acl = acl_hosts - egress_hosts
    only_in_cnp = egress_hosts - acl_hosts
    drift = (
        "haku-egress-proxy allowlist drift — squid.conf `acl allowed dstdomain` and the pod egress CNP "
        "`toFQDNs matchName` must be the same set of FQDNs.\n"
        f"  allowed at L7 (squid.conf) but not reachable (CNP): {sorted(only_in_acl)}\n"
        f"  reachable (CNP) but not allowed at L7 (squid.conf): {sorted(only_in_cnp)}"
    )
    assert not only_in_acl, drift
    assert not only_in_cnp, drift


if __name__ == "__main__":
    pytest_bazel.main()
