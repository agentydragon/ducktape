"""The FQDN fences on the agent egress proxies: one CiliumNetworkPolicy per proxy Pod naming
what it may resolve and connect to on the public internet, each in its own chart so the
committed file keeps the name its hand-written predecessor had. `agents/mitmproxy` is written
by `mitmproxy.py`.

A fence bounds the proxy Pod, not the sandboxes behind it, whose force-proxy
CiliumClusterwideNetworkPolicies admit kube-dns as a plain L4 rule. In-cluster traffic is not
fenced either: the `cluster` entity expands to remote-node and host, so that rule admits every
in-cluster Service and every service published on a node IP -- all of *.allegedly.works,
named or not -- because in-cluster services authenticate their own callers. A
*.allegedly.works name in a toFQDNs group therefore grants nothing (node identities,
cluster/docs/cilium_network_policy.md); it is kept because the DNS half of the fence does
bound the name. Known gaps: cluster/k8s/TODO.md § Egress fences.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cdk8s import App, Chart
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress

from cluster.cdk8s import cilium
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy, dns_allowlist

HAKU_EGRESS_PROXY_NAMESPACE = "haku-egress-proxy"
MITMPROXY_NAMESPACE = "agents-mitmproxy"

# In-cluster names, deliberately unfenced. `**.` is Cilium's subdomain wildcard (one or more
# whole labels; a single `*` stops at the label boundary), so this covers in-cluster names and
# the search-path probes glibc emits before every external lookup (ndots:5 tries
# `github.com.<ns>.svc.cluster.local` first). Those must resolve to a real NXDOMAIN; a policy
# REFUSED is not the same thing to a resolver.
CLUSTER_DNS = ("**.cluster.local",)

# What haku-egress-proxy, the haku-sandbox and haku-ci chokepoint, reaches on the public
# internet: one toFQDNs rule per group.
_HAKU_CLOUD_API_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        # Container image registries (base-image + tooling pulls: dind base images, ghcr
        # tools). Docker Hub is absent: haku-ci's dind pulls it through the in-cluster
        # oci-cache Zot mirror (`--registry-mirror`, cluster/k8s/oci-cache/), which fetches
        # from origin under its own namespace egress. ghcr stays until dind can mirror it too
        # (needs containerd hosts.toml / buildkit -- classic dockerd only mirrors Docker Hub;
        # oci-cache README "Phase 2").
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
        # Claude Agent SDK smoke/runtime telemetry. The sandbox CLI subprocess removes
        # *.allegedly.works from its inherited NO_PROXY so this public Authentik-gated endpoint
        # stays behind the forced proxy.
        "alloy-otlp.allegedly.works",
        # Haku mailbox read API (bearer-gated, read-only; haku/mailbox/README.md) so sandbox
        # pods can poll inbound operator mail.
        "haku-mailbox.allegedly.works",
        # Coinbase read-only source: Haku reads the operator's crypto balances (CDP Advanced
        # Trade get_accounts) from a haku-sandbox pod -- Plaid does not support Coinbase as a
        # readable institution -- with the ESO-distributed read-only `haku-sandbox-coinbase-api-credentials`
        # (cluster-sops-read) CDP key, and marks ETH/BTC to USD via the public /v2/prices
        # endpoint on the same host.
        "api.coinbase.com",
    ),
    # Package registries for ad-hoc tooling installs inside sandbox pods (`pip install
    # fastmcp`, `npx <mcp-server>`): PyPI index/metadata + wheels, npm. The egress proxy's HTTP
    # cache speeds up repeat fetches but does not let these leave the allowlist -- the proxy
    # still fetches from origin. Dropping them needs a dedicated pull-through mirror with its
    # own egress (devpi/bandersnatch for PyPI, Verdaccio for npm); TODO(pull-through-cache).
    ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org"),
    # Nix binary cache + channels, so Haku runtimes that install/refresh their Nix closure hit
    # the public cache instead of building from source. cache.nixos.org serves nars; nixos.org
    # redirects channel/installer fetches.
    # TODO(pull-through-cache): cache.nixos.org can leave once clients use the cluster's own
    #   nix cache (cache.allegedly.works) as an upstream substituter -- that cache reaches
    #   nixos.org under its own egress. (nixos.org / channels.nixos.org are redirect/channel
    #   fetches, not nar content, so they stay unless the pin is fully flake-locked.)
    ("cache.nixos.org", "nixos.org", "channels.nixos.org"),
    # The haku-ci Bazel cold-fetch closure (no RBE): Bazel registry + release artifacts,
    # GitHub source/release archives, GNU source archives (rules_oci pulls gawk for
    # py_image_layer manifests), the Node.js toolchain, snapshot.debian.org (rules_distroless
    # apt manifests in haku-state -- the jupyter sidecar's git layer -- resolve + fetch debs
    # from the dated snapshot at Bazel fetch time, on haku-ci and in Haku's sandboxes alike),
    # and the Rust toolchain + crates: augur's simulator is a Rust extension, so anything
    # depending on `@ducktape//finance/augur/rust` makes rules_rust cold-fetch rustc and its
    # crates.
    (
        # keep-sorted start
        "bcr.bazel.build",
        "codeload.github.com",
        "ftp.gnu.org",
        "github.com",
        "index.crates.io",
        "nodejs.org",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "releases.bazel.build",
        "snapshot.debian.org",
        "static.crates.io",
        "static.rust-lang.org",
        # keep-sorted end
    ),
    # Forgejo Actions: `uses:` actions from data.forgejo.org, act_runner + job-container images
    # from code.forgejo.org.
    ("code.forgejo.org", "data.forgejo.org"),
    # Managed Agents self-hosted worker (haku/runtime/managed_agent/self_hosted) long-polls
    # Anthropic's work queue via `ant beta:worker poll`.
    ("api.anthropic.com",),
    # AnkiWeb sync (haku-anki service, haku/plans in haku-state): sync.ankiweb.net plus the
    # shard hosts it 308-redirects to -- Anki's own firewall guidance is to allow *.ankiweb.net
    # because hostnames change. Client is rslib's reqwest: it honors HTTPS_PROXY and
    # rustls-native-certs reads SSL_CERT_FILE. Kyverno injects the trust-manager bundle
    # (default roots + haku proxy CA) into every haku-sandbox container, so AnkiWeb stays
    # TLS-intercepted rather than using --ignore-hosts.
    ("*.ankiweb.net",),
)

# The openclaw spike proxy's iron `allowlist` transform (haku_egress_proxy.py), which bounds it
# at L7; the DNS rule built from it is the fence's second layer.
# forgejo-http.forgejo is listed for parity with the iron allowlist; it is never queried in that
# form, since the search path resolves it as forgejo-http.forgejo.svc.cluster.local first.
OPENCLAW_SPIKE_ALLOWLIST = (
    # keep-sorted start
    "api.anthropic.com",
    "api.github.com",
    "bcr.bazel.build",
    "cache.nixos.org",
    "channels.nixos.org",
    "code.forgejo.org",
    "codeload.github.com",
    "data.forgejo.org",
    "files.pythonhosted.org",
    "forgejo-http.forgejo",
    "ftp.gnu.org",
    "ghcr.io",
    "github.com",
    "haku.allegedly.works",
    "index.crates.io",
    # The kube-apiserver, via the terminate+re-encrypt Gateway route (kube-api-proxy/README.md).
    # A fence gaining this host is a privilege change: what its holder may do is decided by the
    # RBAC bound to the identity in its bearer token, not by this list.
    "kubeapi.allegedly.works",
    "nixos.org",
    "nodejs.org",
    "objects.githubusercontent.com",
    "pkg-containers.githubusercontent.com",
    "pypi.org",
    "raw.githubusercontent.com",
    "registry.npmjs.org",
    "release-assets.githubusercontent.com",
    "releases.bazel.build",
    "snapshot.debian.org",
    "static.crates.io",
    "static.rust-lang.org",
    # keep-sorted end
)

# What the shared agents-mitmproxy, the claude-sandbox chokepoint, reaches on the public
# internet.
_MITMPROXY_GROUPS: tuple[tuple[str, ...], ...] = (
    # The build-registry bucket: public package, source and toolchain registries, granted
    # all-or-none (//cluster/cdk8s:test_egress_fences).
    (
        "bcr.bazel.build",
        "cache.nixos.org",
        "channels.nixos.org",
        "code.forgejo.org",
        "codeload.github.com",
        "data.forgejo.org",
        "files.pythonhosted.org",
        "ftp.gnu.org",
        "ghcr.io",
        "github.com",
        "index.crates.io",
        "nixos.org",
        "nodejs.org",
        "objects.githubusercontent.com",
        "pkg-containers.githubusercontent.com",
        "pypi.org",
        "raw.githubusercontent.com",
        "registry.npmjs.org",
        "release-assets.githubusercontent.com",
        "releases.bazel.build",
        "snapshot.debian.org",
        "static.crates.io",
        "static.rust-lang.org",
    ),
    # Model provider APIs reached directly from sandbox pods.
    ("generativelanguage.googleapis.com", "api.openai.com", "api.anthropic.com"),
)


def _fence(
    app: App, file_stem: str, *, name: str, namespace: str, proxy: str, egress: Sequence[CiliumNetworkPolicySpecEgress]
) -> Chart:
    """One policy on the Pods labelled `proxy`, in a chart named after the file it becomes.
    Additive with the namespace's default-deny NetworkPolicy."""
    chart = Chart(app, file_stem, disable_resource_name_hashes=True)
    NetworkPolicy(
        chart, "fence", metadata=metadata(name, namespace), selector={"app.kubernetes.io/name": proxy}, egress=egress
    )
    return chart


def haku_cloud_api(app: App) -> Chart:
    """The haku-egress-proxy fence. Plaid Postgres is reached cluster-internally, not through
    this proxy: the `cluster` rule of haku_egress_proxy.py's haku-sandbox-force-proxy-egress."""
    return _fence(
        app,
        "cnp-haku-cloud-api-egress",
        name="allow-haku-cloud-api-egress",
        namespace=HAKU_EGRESS_PROXY_NAMESPACE,
        proxy="haku-egress-proxy",
        egress=[
            *cilium.fqdn_fence(*_HAKU_CLOUD_API_GROUPS, resolves_also=CLUSTER_DNS),
            # Forwarding to any in-cluster Service: sandbox workloads keep the proxy in-path even
            # when targeting cluster endpoints (NO_PROXY in the inject policy still permits
            # explicit bypass for `*.svc.cluster.local` and `10.0.0.0/8`). The widest rule in
            # this fence, deliberately -- see the module docstring.
            EgressRule.to_entities(Entity.CLUSTER, ports=[80, 443, 8000, 8080, 11434]),
        ],
    )


def haku_openclaw_spike(app: App) -> Chart:
    """The spike's iron proxy. DNS is its only Cilium-side allowlist: the 443 rule below cannot
    be one, and unlike toFQDNs the DNS rule can express haku.allegedly.works, because it
    matches before any destination identity exists."""
    return _fence(
        app,
        "openclaw-spike-cnp-egress",
        name="allow-haku-openclaw-spike-proxy-egress",
        namespace=HAKU_EGRESS_PROXY_NAMESPACE,
        proxy="haku-openclaw-spike-proxy",
        egress=[
            cilium.dns_egress(protocols=["ANY"], resolves=dns_allowlist(*OPENCLAW_SPIKE_ALLOWLIST, *CLUSTER_DNS)),
            # Public HTTPS is application-layer allowlisted by iron-proxy. remote-node/host
            # because allegedly.works is served from node ExternalIPs, which Cilium does not
            # classify as world. This opens 443 to anything and cannot be narrowed into an
            # allowlist: a toFQDNs list would fence true-world hosts but could not express
            # haku.allegedly.works (node identity, cluster/docs/cilium_network_policy.md). What
            # bounds this proxy is the iron allowlist at L7 and the DNS rule above -- neither of
            # which stops a destination reached by literal IP.
            EgressRule.to_entities(Entity.WORLD, Entity.REMOTE_NODE, Entity.HOST, ports=[443]),
            EgressRule.to_endpoints(
                {"k8s:io.kubernetes.pod.namespace": "forgejo", "k8s:app.kubernetes.io/name": "forgejo"}, 3000
            ),
        ],
    )


def mitmproxy_cloud_api(app: App) -> Chart:
    return _fence(
        app,
        "cnp-cloud-api-egress",
        name="allow-cloud-api-egress",
        namespace=MITMPROXY_NAMESPACE,
        proxy="mitmproxy",
        egress=[
            *cilium.fqdn_fence(*_MITMPROXY_GROUPS, resolves_also=CLUSTER_DNS),
            # Forwarding to any in-cluster Service: sandbox bench Jobs and other workloads keep
            # mitmproxy in-path even when targeting cluster endpoints (e.g. `ollama.ollama:11434`);
            # NO_PROXY in the inject policy still permits explicit bypass for
            # `*.svc.cluster.local` and `10.0.0.0/8`.
            EgressRule.to_entities(Entity.CLUSTER, ports=[11434, 80, 443, 8000, 8080]),
        ],
    )


def write_manifests(root: Path) -> None:
    write_charts(root, f"{HAND_WRITTEN_ROOT}/agents/haku-egress-proxy", haku_cloud_api, haku_openclaw_spike)
