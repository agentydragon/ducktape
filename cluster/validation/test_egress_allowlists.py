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

Each fence is pinned twice, against the same declared set: once on where it may
*connect* (`toFQDNs`, or the iron `allowlist` transform) and once on what it may
*look up* (`toPorts.rules.dns`). The two are separate enforcement points, not one
restated — the DNS proxy matches the query name before any destination identity
exists, which is what lets it fence a `*.allegedly.works` name that `toFQDNs`
cannot. Cilium has no way to share one list between the rule kinds, so the
equality assertion is what keeps the copies from drifting.

Both halves bound **the proxy pod**, not the workloads behind it. See the DNS
gap under Known gaps: the sandboxes reach kube-dns through a rule with no
`rules.dns` at all, so their own resolution is not fenced by anything here.

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
  node identities (`cluster/docs/cilium_network_policy.md`). The entries pinned
  below that hit this — `alloy-otlp`, `haku-mailbox`, `aiquota`, `docker-ci` —
  record intent at that layer; all but `docker-ci` are admitted by a `toEntities`
  rule instead, while `docker-ci` appears to be a dead rule. What makes them
  load-bearing anyway is the DNS half of the pin, which does fence them.

Known gaps
----------

What the pins above cover is one slice: the host set each proxy may reach. These
are the things known to be wrong or unverified around them as of 2026-08-04.
Recorded here because this is the file anyone touching a fence opens; the
operator-facing version is in `haku/docs/security.md`.

- TODO: collapse the fences. Six manifests express about three distinct
  policies — one host (`api.anthropic.com`), build registries plus a model API,
  and the operator-data tier.
  `agents/public-coder-agent` is a waiver rather than a fence. Done when the
  dict below has one entry per policy, not one per proxy deployment.
- TODO: converge on one proxy. mitmproxy (`agents/haku-egress-proxy`,
  `agents/mitmproxy`) has no credential
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
  to `*.allegedly.works`, `.svc`, `.svc.cluster.local` and
  `10.0.0.0/8`, so anything under the operator's own domains or the cluster
  network is reached with no proxy in the path and no allowlist applied. It is
  also why the same service is referenced by its public name in one manifest and
  its cluster name in another.
- Allowlist DNS on the **proxies**: done, pinned by
  `test_dns_rule_matches_the_allowlist`. `public-coder-agent` stays on
  `matchPattern: "*"` because narrowing DNS alone fences nothing while its
  `toEntities: [world]` rule stands, and `**.cluster.local` stays open by design
  — the same in-cluster posture as the `toEntities` rule.
- TODO: allowlist DNS for the **sandboxes**, which is where the exfil channel
  actually is. Verified in-cluster 2026-08-04: the force-proxy CCNPs admit
  sandbox pods to kube-dns with a plain L4 rule and no `rules.dns`, so Cilium
  does not proxy their queries at all and they resolve anything. A probe pod in
  `haku-sandbox` resolved `example.com` while the same name 502'd through the
  proxy — the fence stops the connection, not the lookup, and a lookup under an
  attacker-controlled zone still reaches that zone's nameserver. Recorded as
  accepted rather than fixed in `haku/docs/security.md`; the fix belongs in the
  force-proxy CCNPs, not in the allowlists pinned here.
- TODO: record traffic, including rejections. The proxies filter and substitute
  credentials but keep no request log, so there is no record of what an agent
  actually reached and a blocked request is invisible after the fact. Rejections
  are the more valuable half: they are how a fence being too tight, or an agent
  trying somewhere it should not, becomes observable at all.

Deliberately out of scope here: which pod is *routed* through which proxy. That
pairing lives in the force-proxy `CiliumClusterwideNetworkPolicy` manifests
(`agents/haku-egress-proxy/ccnp-haku-{proxy,claude-sandbox}-egress.yaml`,
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


# GitHub's source and release-artifact hosts. Split out of the build bucket because
# "reads source, builds nothing" is a real posture — the Claude runner pool holds these
# and no package registry — and that is a decision rather than the drift the all-or-none
# rule exists to catch. Still one trust level internally, so it is all-or-none in its own
# right. Anonymous these are read-only; a fence that also substitutes a credential for
# them (see haku-claude) turns them into a push surface.
GITHUB_GIT = hosts(
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
)

# The shared build-registry bucket: public package, source and toolchain
# registries. Every fence that builds code carries the whole set — see
# `test_build_registries_are_all_or_none`. They share one trust level (public,
# read-only, credential-gated to publish to), so splitting them into nine groups
# bought a precision nobody used while letting the fences quietly drift apart.
BUILD_REGISTRIES = GITHUB_GIT | hosts(
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

# aiquota's read API, reached only by the console Claude runner pool for its own
# AI-usage quotas. A `*.allegedly.works` name, so its `toFQDNs` half enforces
# nothing (node IPs); the `toEntities` rule admits the connection and the DNS half
# fences the name — see the module docstring's node-IP carve-out.
AIQUOTA = hosts("aiquota.allegedly.works")

# The central ActivityWatch read API, reached only by the console Claude runner pool
# so the agent can query the operator's own activity history. Same node-IP
# `*.allegedly.works` case as aiquota: the `toFQDNs` half enforces nothing, the
# `toEntities` rule admits, the DNS half fences. Read-only at the route (bearer-proxy
# allows GET + query-POST only) with the bearer substituted at the proxy, so the
# sandbox never holds it. Its own group, not OPERATOR_DATA: window/AFK/app history is
# what makes the agent useful on this fence, deliberately kept apart from the mail /
# calendar / finance tier that stays fenced to the general haku-sandbox.
ACTIVITYWATCH = hosts("activitywatch-read.allegedly.works")

# The cluster's own API server, through the terminate+re-encrypt Gateway route.
# Its own group because reaching it is not like reaching a registry: what a
# holder may actually do is decided by the RBAC bound to the identity in its
# bearer token, not by this list. A fence gaining this host is a privilege
# change and should read as one in the diff.
KUBE_API = hosts("kubeapi.allegedly.works")

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


# In-cluster names, deliberately unfenced: services there authenticate their own
# callers, so reachability is not the boundary. `**.` is Cilium's subdomain
# wildcard (one or more whole labels), which a single `*` cannot express — and
# the depth matters, because with ndots:5 every external lookup first probes
# `github.com.<ns>.svc.cluster.local`. Those probes must reach CoreDNS and come
# back NXDOMAIN; a policy REFUSED is not the same answer to a resolver.
CLUSTER_DNS = hosts("**.cluster.local")


@dataclass(frozen=True)
class Confined:
    """A Cilium fence: `toFQDNs` and the DNS rule both live in the keyed manifest."""

    allows: frozenset[str]
    # Names the fence may resolve beyond `allows`. Empty where the pod has no
    # in-cluster egress at all, so it has no business resolving cluster names.
    resolves_also: frozenset[str] = CLUSTER_DNS


@dataclass(frozen=True)
class IronConfined:
    """An iron-proxy fence: the host list is app config, the DNS rule is Cilium's.

    Two files, because the two layers are enforced by different components. The
    DNS rule is the only Cilium-side bound on these proxies, so it is pinned to
    the same set rather than left open.
    """

    allows: frozenset[str]
    dns_manifest: str
    resolves_also: frozenset[str] = CLUSTER_DNS


@dataclass(frozen=True)
class Unconfined:
    """A proxy deliberately permitted to reach the whole internet."""

    reason: str
    allows: frozenset[str] = field(default=frozenset(), init=False)


# The fences the assertions below single out. They are the dict keys
# themselves, so the assertion and the entry cannot drift apart.
OPERATOR_DATA_FENCE = "agents/haku-egress-proxy/cnp-haku-cloud-api-egress.yaml"
HAKU_CLAUDE_FENCE = "agents/haku-egress-proxy/cnp-haku-claude-egress.yaml"
HAKU_OPENCLAW_FENCE = "agents/haku-egress-proxy/openclaw-spike-iron.yaml"

# Keyed by manifest path: the file is the fence's only real identifier, so there
# is no second name to keep in sync, and a failure names the file to open.
ALLOWLISTS: dict[str, Confined | IronConfined | Unconfined] = {
    # Haku's general sandbox — the only fence holding OPERATOR_DATA.
    OPERATOR_DATA_FENCE: Confined(
        allows=BUILD_REGISTRIES | ANTHROPIC | OPERATOR_DATA | hosts("alloy-otlp.allegedly.works")
    ),
    # Haku's OpenClaw + Claude Code spike (namespace `haku-openclaw-spike`),
    # through its own iron proxy. Not public-coder-agent, which runs the same
    # OpenClaw image behind a different fence.
    HAKU_OPENCLAW_FENCE: IronConfined(
        allows=BUILD_REGISTRIES
        | ANTHROPIC
        | GITHUB_API
        | KUBE_API
        | hosts("forgejo-http.forgejo", "haku.allegedly.works"),
        dns_manifest="agents/haku-egress-proxy/openclaw-spike-cnp-egress.yaml",
    ),
    # The console-owned Claude runner pool (`haku-runtime-sandbox`). Still among the
    # tightest fences in the cluster — no registries, because it builds nothing — but no
    # longer one host. It reaches GitHub as `agentydragon-agent`, with the PAT substituted
    # at the proxy so the sandbox never holds it — a write grant to every repo the account
    # can touch, narrowing to named repos tracked in `haku/TODO.md`. It also reaches
    # aiquota's read API for its own usage quotas and the ActivityWatch read API for the
    # operator's activity history (both bearers likewise substituted); those are node-IP
    # `*.allegedly.works` names, so — unlike the GitHub hosts, which `toFQDNs` genuinely
    # fences — they are admitted by the `toEntities` rule and fenced by the DNS half.
    # It reaches nothing by cluster name, hence no cluster DNS.
    HAKU_CLAUDE_FENCE: Confined(
        allows=ANTHROPIC | GITHUB_API | GITHUB_GIT | AIQUOTA | ACTIVITYWATCH, resolves_also=frozenset()
    ),
    # The `claude-sandbox` namespace, via the shared agents-mitmproxy.
    "agents/mitmproxy/cnp-cloud-api-egress.yaml": Confined(
        allows=BUILD_REGISTRIES
        | ANTHROPIC
        | hosts("api.openai.com", "generativelanguage.googleapis.com", "docker-ci.allegedly.works")
    ),
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


def _cilium_dns_names(document: dict) -> set[str]:
    """Every name a `toPorts.rules.dns` rule permits the pod to look up."""
    return {
        name
        for rule in document["spec"]["egress"]
        for port_rule in rule.get("toPorts", ())
        for entry in port_rule.get("rules", {}).get("dns", ())
        for name in entry.values()
    }


def _iron_hosts(document: dict) -> set[str]:
    allowlists = [t for t in document["transforms"] if t["name"] == "allowlist"]
    return set(allowlists[0]["config"]["domains"]) if allowlists else set()


def _load(k8s_dir: Path, path: str) -> dict:
    document: dict = yaml.safe_load((k8s_dir / path).read_text())
    return document


@pytest.mark.parametrize("path", sorted(ALLOWLISTS))
def test_allowlist_matches_its_declared_host_set(path: str, k8s_dir: Path) -> None:
    allowlist = ALLOWLISTS[path]
    document = _load(k8s_dir, path)
    if isinstance(allowlist, Unconfined):
        # The waiver itself is pinned: it must reach `world` and must name no
        # hosts, so restoring confinement is a visible change either way.
        assert _cilium_hosts(document) == set()
        assert "world" in _cilium_entities(document)
        return
    actual = _iron_hosts(document) if isinstance(allowlist, IronConfined) else _cilium_hosts(document)
    assert actual == set(allowlist.allows)


@pytest.mark.parametrize("path", sorted(ALLOWLISTS))
def test_dns_rule_matches_the_allowlist(path: str, k8s_dir: Path) -> None:
    """A fence may look up exactly what it may connect to, and cluster names.

    The DNS rule is a second enforcement layer, not a restatement: it matches the
    query name before any destination identity exists, so it is the only thing
    that bounds a `*.allegedly.works` name (`toFQDNs` cannot select node
    identities). It bounds the proxy pod's own resolution — not the sandboxes',
    which nothing fences (Known gaps).
    Cilium cannot share one list between the two rule kinds, so this assertion is
    what keeps the copies equal.
    """
    allowlist = ALLOWLISTS[path]
    if isinstance(allowlist, Unconfined):
        # An unconfined proxy resolves anything; narrowing DNS alone would fence
        # nothing while breaking the waiver.
        assert _cilium_dns_names(_load(k8s_dir, path)) == {"*"}
        return
    manifest = allowlist.dns_manifest if isinstance(allowlist, IronConfined) else path
    assert _cilium_dns_names(_load(k8s_dir, manifest)) == set(allowlist.allows | allowlist.resolves_also)


def test_build_registries_are_all_or_none() -> None:
    """No fence carries part of the bucket.

    The hosts are grouped because they share a trust level, so a fence holding a
    subset is drift rather than a decision — which is exactly how the fences
    diverged before they were pinned.
    """
    for path, allowlist in ALLOWLISTS.items():
        # Per bucket: GITHUB_GIT is separable from the package registries (a fence may read
        # source without building), but neither bucket may be held in part.
        for bucket in (GITHUB_GIT, BUILD_REGISTRIES - GITHUB_GIT):
            held = allowlist.allows & bucket
            assert held in (frozenset(), bucket), path


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
    """`api.github.com` is a write surface, so every grant is named explicitly."""
    holders = {path for path, entry in ALLOWLISTS.items() if entry.allows & GITHUB_API}
    assert holders == {HAKU_OPENCLAW_FENCE, HAKU_CLAUDE_FENCE}


if __name__ == "__main__":
    pytest_bazel.main()
