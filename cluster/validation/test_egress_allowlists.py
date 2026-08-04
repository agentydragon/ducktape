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

Entries are keyed by manifest path — the fence's only real identifier, so there
is no second name that could disagree with it, and a failure names the file to
open.

There are four groups, and only four axes on which the fences differ: whether a
fence builds code (`BUILD_REGISTRIES`), which model APIs it reaches, whether it
can write to GitHub (`GITHUB_API`), and whether it touches the operator's own
accounts (`OPERATOR_DATA`). Everything else is one or two named hosts.

`toFQDNs` entries only. The `matchPattern: "*"` under `toPorts.rules.dns` is a
DNS *query* filter, not an egress allowlist.

What a pinned host set does and does not mean
---------------------------------------------

These sets bound each proxy's reach **on the public internet**, and nothing
else. Two carve-outs are deliberate and neither is visible from the host lists
alone:

- *In-cluster traffic is not fenced.* The mitmproxy fences carry a
  `toEntities: [cluster]` rule, and `cluster` expands to include `remote-node`
  and `host` — so they admit every in-cluster Service and every service
  published on a node IP, which is all of `*.allegedly.works`. That is the
  intended posture: in-cluster services authenticate their own callers, so
  reachability is not where that boundary is drawn.
- *A `*.allegedly.works` entry in a `toFQDNs` block enforces nothing.* Those
  names resolve to the hostNetwork Gateway node IPs, and `toFQDNs` cannot select
  node identities (`cluster/docs/cilium_network_policy.md`). The three such
  entries pinned below (`alloy-otlp`, `haku-mailbox`, `docker-ci`) record intent;
  the first two are admitted by the `toEntities` rule instead, and the third
  appears to be a dead rule. They stay pinned because the DNS layer *can* fence
  them, so the recorded intent is what a DNS allowlist would be built from.

Known gaps
----------

What the pins above cover is one slice: the host set each proxy may reach. These
are the things known to be wrong or unverified around them as of 2026-08-04.
Recorded here because this is the file anyone touching a fence opens; the
operator-facing version is in `haku/docs/security.md`.

- TODO: collapse the fences. Six manifests express about three distinct
  policies — one host (`api.anthropic.com`), build registries plus a model API,
  and the operator-data tier. `agents/haku-zones-mitmproxy` fences a single
  namespace (`haku-sandbox-zai`) whose future is undecided, and
  `agents/public-coder-agent` is a waiver rather than a fence. Done when the
  dict below has one entry per policy, not one per proxy deployment.
- TODO: converge on one proxy. mitmproxy (`agents/haku-egress-proxy`,
  `agents/mitmproxy`, `agents/haku-zones-mitmproxy`) has no credential
  placeholders, so `haku-sandbox` sends real unredacted tokens upstream where
  iron would send a placeholder and substitute in a trusted pod. Moving those to
  iron is blocked on three mitmproxy behaviours whose iron equivalents are
  unverified: `--set stream_large_bodies=1m` (dind image layers were buffered
  whole into memory and OOM-killed the pod), and two `--ignore-hosts` raw TLS
  passthroughs — `api.anthropic.com`, because interception breaks the Managed
  Agents HTTP/2 session stream, and `docker-ci.allegedly.works`, because docker
  mTLS must reach the daemon end to end. Verify those first.
- TODO: enforce at two layers, not one. Every fence today is single-layer.
  The mitmproxy fences confine only via Cilium `toFQDNs` — the mitmproxy
  container itself has no allowlist. The iron fences confine only in app config:
  `openclaw-spike-cnp-egress.yaml` opens `toEntities: [world, remote-node,
  host]` on 443, and `claude-iron.yaml` carries a `secrets` transform with no
  `allowlist`. Defence in depth wants both wherever iron's allowlist and a
  Cilium FQDN policy can express the same set.
- TODO: route cluster-internal traffic through the proxies too. The Kyverno
  injection (`kyverno/policies/inject-haku-egress-proxy.yaml`) sets `NO_PROXY`
  to `*.allegedly.works`, `*.forgejo`, `.svc`, `.svc.cluster.local` and
  `10.0.0.0/8`, so anything under the operator's own domains or the cluster
  network is reached with no proxy in the path and no allowlist applied. It is
  also why the same service is referenced by its public name in one manifest and
  its cluster name in another.
- TODO: allowlist DNS. Five of the six fences carry `matchPattern: "*"` under
  `toPorts.rules.dns`, so a resolvable name is an egress channel regardless of
  what `toFQDNs` permits. Only `cnp-haku-claude-egress.yaml` pins its DNS rule
  (`matchName: api.anthropic.com`) — that is the shape the others should take.
- TODO: record traffic, including rejections. The proxies filter and substitute
  credentials but keep no request log, so there is no record of what an agent
  actually reached and a blocked request is invisible after the fact. Rejections
  are the more valuable half: they are how a fence being too tight, or an agent
  trying somewhere it should not, becomes observable at all.

Deliberately out of scope here: which pod is *routed* through which proxy. That
pairing lives in the force-proxy `CiliumClusterwideNetworkPolicy` manifests
(`agents/haku-egress-proxy/ccnp-haku-{proxy,claude-sandbox}-egress.yaml`,
`agents/haku-zones-mitmproxy/ccnp-zones-force-proxy-egress.yaml`,
`agents/mitmproxy/ccnp-sandbox-proxy-egress.yaml`,
`haku-ci/ccnp-force-proxy-egress.yaml`) and is unverified — a selector that
matches nothing bypasses the fence entirely while every assertion here stays
green.
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


# The shared build-registry bucket: public package, source and toolchain
# registries. Every fence that builds code carries the whole set — see
# `test_build_registries_are_all_or_none`. They share one trust level (public,
# read-only, credential-gated to publish to), so splitting them into nine groups
# bought a precision nobody used while letting the fences quietly drift apart.
BUILD_REGISTRIES = hosts(
    # Source and release artifacts
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "ftp.gnu.org",
    # Language package registries and toolchains
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "nodejs.org",
    # Nix
    "cache.nixos.org",
    "nixos.org",
    "channels.nixos.org",
    # Bazel
    "releases.bazel.build",
    "bcr.bazel.build",
    # Container images and OS packages
    "ghcr.io",
    "pkg-containers.githubusercontent.com",
    "snapshot.debian.org",
    # Forgejo's own public release/CDN hosts, not the in-cluster instance
    "code.forgejo.org",
    "data.forgejo.org",
)

# A write surface, deliberately outside the bucket: anonymous github.com is
# read-only, while the API is what makes gists, issues and pushes reachable.
# Folding it in would hand every build fence a way to publish at once.
GITHUB_API = hosts("api.github.com")

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

    allows: frozenset[str]
    # Iron-proxy application config rather than a Cilium policy.
    iron_transform: bool = False


@dataclass(frozen=True)
class Unconfined:
    """A proxy deliberately permitted to reach the whole internet."""

    reason: str
    allows: frozenset[str] = field(default=frozenset(), init=False)


# The two fences the assertions below single out. They are the dict keys
# themselves, so the assertion and the entry cannot drift apart.
OPERATOR_DATA_FENCE = "agents/haku-egress-proxy/cnp-haku-cloud-api-egress.yaml"
GITHUB_API_FENCE = "agents/haku-zones-mitmproxy/cnp-zones-egress.yaml"

# Keyed by manifest path: the file is the fence's only real identifier, so there
# is no second name to keep in sync, and a failure names the file to open.
ALLOWLISTS: dict[str, Confined | Unconfined] = {
    # Haku's general sandbox — the only fence holding OPERATOR_DATA.
    OPERATOR_DATA_FENCE: Confined(
        allows=BUILD_REGISTRIES | ANTHROPIC | OPERATOR_DATA | hosts("alloy-otlp.allegedly.works")
    ),
    # Haku's OpenClaw + Claude Code spike (namespace `haku-openclaw-spike`),
    # through its own iron proxy. Not public-coder-agent, which runs the same
    # OpenClaw image behind a different fence.
    "agents/haku-egress-proxy/openclaw-spike-iron.yaml": Confined(
        allows=BUILD_REGISTRIES | ANTHROPIC | hosts("forgejo-http.forgejo", "haku.allegedly.works"), iron_transform=True
    ),
    # The console-owned Claude runner pool (`haku-claude-sandbox`). Deliberately
    # the tightest fence in the cluster — one host, no registries, because it
    # builds nothing. Widening it is a security change.
    "agents/haku-egress-proxy/cnp-haku-claude-egress.yaml": Confined(allows=ANTHROPIC),
    # The `claude-sandbox` namespace, via the shared agents-mitmproxy.
    "agents/mitmproxy/cnp-cloud-api-egress.yaml": Confined(
        allows=BUILD_REGISTRIES
        | ANTHROPIC
        | hosts("api.openai.com", "generativelanguage.googleapis.com", "docker-ci.allegedly.works")
    ),
    # `haku-sandbox-zai`, the one "zone" namespace today —
    # ccnp-zones-force-proxy-egress.yaml selects a list, so a second zone would
    # share this fence rather than get its own.
    GITHUB_API_FENCE: Confined(allows=BUILD_REGISTRIES | GITHUB_API),
    "agents/public-coder-agent/proxy/cnp-egress.yaml": Unconfined(
        reason=(
            "Scoped waiver for this agent only: both its Cilium toFQDNs rules and its "
            "iron allowlist are commented out and kept verbatim for restoration. It "
            "handles no operator data; the confined config is one uncomment away."
        )
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


@pytest.mark.parametrize("path", sorted(ALLOWLISTS))
def test_allowlist_matches_its_declared_host_set(path: str, k8s_dir: Path) -> None:
    allowlist = ALLOWLISTS[path]
    document: dict = yaml.safe_load((k8s_dir / path).read_text())
    if isinstance(allowlist, Unconfined):
        # The waiver itself is pinned: it must reach `world` and must name no
        # hosts, so restoring confinement is a visible change either way.
        assert _cilium_hosts(document) == set()
        assert "world" in _cilium_entities(document)
        return
    actual = _iron_hosts(document) if allowlist.iron_transform else _cilium_hosts(document)
    assert actual == set(allowlist.allows)


def test_build_registries_are_all_or_none() -> None:
    """No fence carries part of the bucket.

    The hosts are grouped because they share a trust level, so a fence holding a
    subset is drift rather than a decision — which is exactly how the fences
    diverged before they were pinned.
    """
    for path, allowlist in ALLOWLISTS.items():
        held = allowlist.allows & BUILD_REGISTRIES
        assert held in (frozenset(), BUILD_REGISTRIES), path


def test_operator_data_reaches_only_haku_sandbox() -> None:
    """The tier that reads the operator's own accounts stays in one fence.

    A coding agent that gained these would turn each of its injection surfaces
    into a path to the operator's mail and finances.
    """
    for path, allowlist in ALLOWLISTS.items():
        if path == OPERATOR_DATA_FENCE:
            continue
        assert not (allowlist.allows & OPERATOR_DATA), path


def test_github_api_reaches_only_declared_holders() -> None:
    """`api.github.com` is a write surface, so it is granted one fence at a time."""
    holders = {path for path, entry in ALLOWLISTS.items() if entry.allows & GITHUB_API}
    assert holders == {GITHUB_API_FENCE}


if __name__ == "__main__":
    pytest_bazel.main()
