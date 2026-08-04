"""Exact pins over every egress allowlist in the cluster.

Six proxies bound what agent workloads reach. Five enforce a host list — four
Cilium `toFQDNs` policies plus the openclaw spike's app-level iron transform —
and one (`public-coder`) is a deliberate, documented waiver that reaches the
whole internet. Before this test they had drifted into near-but-not-quite-equal
host sets, and nothing failed when one gained an entry.

Each allowlist declares the exact set it may carry, as a union of named host
groups. The assertion is equality, so no host can be added, removed or renamed
anywhere without this test failing. That is not the change-detector `STYLE.md`
forbids: the value under test is not a literal copied back from the manifest, it
is the claim *"this consumer reaches these groups and nothing else."* A host
added without saying which group it joins is exactly the failure being caught.

Unions only — a consumer that carries part of a group names those hosts
directly rather than subtracting from a larger set, so what a fence permits is
read top-to-bottom in one place.

The pin records what is **deployed**, not what ought to be. Consumers hold
overlapping-but-unequal slices of the build groups; deciding which of those
differences are deliberate is follow-up work, and this test is what stops the
baseline moving underneath it.

`toFQDNs` entries only. The `matchPattern: "*"` under `toPorts.rules.dns` is a
DNS *query* filter, not an egress allowlist; it is a known gap recorded in
`haku/docs/security.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


def hosts(*names: str) -> frozenset[str]:
    """A named host group, kept one indent deep at the call site."""
    return frozenset(names)


GITHUB_SOURCE = hosts("github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com")
GITHUB_RELEASE_ASSETS = hosts("release-assets.githubusercontent.com")
# A write surface: anonymous github.com is read-only, while the API is what
# makes gists, issues and pushes reachable. Separate so granting it is explicit.
GITHUB_API = hosts("api.github.com")
LANGUAGE_REGISTRIES = hosts("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
TOOLCHAIN_DOWNLOADS = hosts("nodejs.org", "ftp.gnu.org")
NIX = hosts("cache.nixos.org", "nixos.org", "channels.nixos.org")
BAZEL = hosts("releases.bazel.build", "bcr.bazel.build")
CONTAINER_REGISTRIES = hosts("ghcr.io", "pkg-containers.githubusercontent.com")
OS_PACKAGES = hosts("snapshot.debian.org")
# Forgejo's own public release/CDN hosts, not the in-cluster instance.
FORGEJO_RELEASES = hosts("code.forgejo.org", "data.forgejo.org")

ANTHROPIC = hosts("api.anthropic.com")

# Hosts serving the operator's own accounts. A prompt-injected agent holding
# these reads the operator's mail, calendar, tasks, finances and study data, so
# the group is named to keep `test_operator_data_reaches_only_haku_sandbox`
# honest as consumers are added.
OPERATOR_DATA = hosts(
    "www.googleapis.com",
    "gmail.googleapis.com",
    "tasks.googleapis.com",
    "api.coinbase.com",
    "haku-mailbox.allegedly.works",
    "*.ankiweb.net",
)


@dataclass(frozen=True)
class Confined:
    """An allowlist pinned to exactly the hosts it declares."""

    path: str
    allows: frozenset[str]
    # Iron-proxy application config rather than a Cilium policy.
    iron_transform: bool = False


@dataclass(frozen=True)
class Unconfined:
    """A proxy deliberately permitted to reach the whole internet."""

    path: str
    reason: str
    allows: frozenset[str] = field(default=frozenset(), init=False)


ALLOWLISTS: dict[str, Confined | Unconfined] = {
    # Haku's general sandbox — the only consumer holding OPERATOR_DATA, and the
    # only one carrying every build group.
    "haku-sandbox": Confined(
        path="agents/haku-egress-proxy/cnp-haku-cloud-api-egress.yaml",
        allows=GITHUB_SOURCE
        | GITHUB_RELEASE_ASSETS
        | LANGUAGE_REGISTRIES
        | TOOLCHAIN_DOWNLOADS
        | NIX
        | BAZEL
        | CONTAINER_REGISTRIES
        | OS_PACKAGES
        | FORGEJO_RELEASES
        | ANTHROPIC
        | OPERATOR_DATA
        | hosts("alloy-otlp.allegedly.works"),
    ),
    # The OpenClaw + Claude Code spike. Its two singleton hosts are in-cluster
    # services it reaches through iron rather than directly.
    "openclaw-spike": Confined(
        path="agents/haku-egress-proxy/openclaw-spike-iron.yaml",
        allows=GITHUB_SOURCE
        | GITHUB_RELEASE_ASSETS
        | LANGUAGE_REGISTRIES
        | TOOLCHAIN_DOWNLOADS
        | NIX
        | BAZEL
        | OS_PACKAGES
        | FORGEJO_RELEASES
        | ANTHROPIC
        | hosts("forgejo-http.forgejo", "haku.allegedly.works"),
        iron_transform=True,
    ),
    # Console-owned Claude runner: deliberately the tightest fence in the
    # cluster — one host. Widening it is a security change.
    "claude-runner": Confined(path="agents/haku-egress-proxy/cnp-haku-claude-egress.yaml", allows=ANTHROPIC),
    # Predates the build groups: it has `github.com` but none of the hosts that
    # serve tarballs or raw files, so `git clone` works here and release fetches
    # silently do not.
    "claude-sandbox": Confined(
        path="agents/mitmproxy/cnp-cloud-api-egress.yaml",
        allows=hosts("github.com")
        | LANGUAGE_REGISTRIES
        | ANTHROPIC
        | hosts("api.openai.com", "generativelanguage.googleapis.com", "docker-ci.allegedly.works"),
    ),
    "zones": Confined(
        path="agents/haku-zones-mitmproxy/cnp-zones-egress.yaml",
        allows=GITHUB_SOURCE | GITHUB_API | LANGUAGE_REGISTRIES,
    ),
    "public-coder": Unconfined(
        path="agents/public-coder-agent/proxy/cnp-egress.yaml",
        reason=(
            "Scoped waiver for this agent only: both its Cilium toFQDNs rules and its "
            "iron allowlist are commented out and kept verbatim for restoration. It "
            "handles no operator data; the confined config is one uncomment away."
        ),
    ),
}


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def _cilium_hosts(document: dict) -> set[str]:
    """Every host named by a `toFQDNs` rule, ignoring DNS query patterns."""
    return {host for rule in document["spec"]["egress"] for entry in rule.get("toFQDNs", ()) for host in entry.values()}


def _cilium_entities(document: dict) -> set[str]:
    return {entity for rule in document["spec"]["egress"] for entity in rule.get("toEntities", ())}


def _iron_hosts(document: dict) -> set[str]:
    allowlists = [t for t in document["transforms"] if t["name"] == "allowlist"]
    return set(allowlists[0]["config"]["domains"]) if allowlists else set()


@pytest.mark.parametrize("name", sorted(ALLOWLISTS))
def test_allowlist_matches_its_declared_host_set(name: str, k8s_dir: Path) -> None:
    allowlist = ALLOWLISTS[name]
    document: dict = yaml.safe_load((k8s_dir / allowlist.path).read_text())
    if isinstance(allowlist, Unconfined):
        # The waiver itself is pinned: it must reach `world` and must name no
        # hosts, so restoring confinement is a visible change either way.
        assert _cilium_hosts(document) == set()
        assert "world" in _cilium_entities(document)
        return
    actual = _iron_hosts(document) if allowlist.iron_transform else _cilium_hosts(document)
    assert actual == set(allowlist.allows)


def test_operator_data_reaches_only_haku_sandbox() -> None:
    """The tier that reads the operator's own accounts stays in one fence.

    A coding agent that gained these would turn each of its injection surfaces
    into a path to the operator's mail and finances.
    """
    for name, allowlist in ALLOWLISTS.items():
        if name == "haku-sandbox":
            continue
        assert not (allowlist.allows & OPERATOR_DATA), name


def test_github_api_reaches_only_declared_holders() -> None:
    """`api.github.com` is a write surface, so it is granted one fence at a time."""
    holders = {name for name, entry in ALLOWLISTS.items() if entry.allows & GITHUB_API}
    assert holders == {"zones"}


if __name__ == "__main__":
    pytest_bazel.main()
