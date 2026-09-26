"""Rules over which egress fence may hold which named host group.

A fence's hosts live once, in its tuple in `egress_fences.py`: its `toFQDNs` groups and its
DNS rule both derive from it, so what a fence holds gets no pin here (editing the tuple is the
review). What stays are the claims no single tuple can express -- the named host groups below
and the rules over which fences may hold them.

The openclaw spike's fence is `OPENCLAW_SPIKE_ALLOWLIST`, which its iron `allowlist` transform
(rendered from the same tuple) enforces at L7. The `public-coder` proxy is a deliberate waiver that holds no list and reaches the
whole internet, so no rule here can bind it.

Every set here bounds a proxy's reach on the public internet and nothing else: in-cluster
traffic is admitted by a `toEntities: cluster` rule, and a `*.allegedly.works` entry in a
`toFQDNs` group enforces nothing (`egress_fences.py`). Known gaps, among them that which pod is
*routed* through which proxy is unverified: `cluster/k8s/TODO.md` § Egress fences.
"""

from __future__ import annotations

import pytest_bazel

from cluster.cdk8s import egress_fences


def hosts(*names: str) -> frozenset[str]:
    """A named host group, kept one indent deep at the call site."""
    return frozenset(names)


# GitHub's source and release-artifact hosts. Split out of the build bucket because "reads
# source, builds nothing" is a real posture, and a fence holding these without the package
# registries is a decision rather than the drift the all-or-none rule exists to catch. Still
# one trust level internally, so it is all-or-none in its own right. Anonymous these are
# read-only; a fence that also substitutes a credential for them (the OpenClaw spike's)
# turns them into a push surface.
GITHUB_GIT = hosts(
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
)

# The shared build-registry bucket: public package, source and toolchain registries. Every
# fence that builds code carries the whole set -- see `test_build_registries_are_all_or_none`.
# They share one trust level (public, read-only, credential-gated to publish to), so splitting
# them into nine groups bought a precision nobody used while letting the fences quietly drift
# apart.
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
    # Rust: the toolchain tarballs and the sparse crate registry. Needed since augur's
    # simulator became a Rust extension, so a consumer that builds `finance/augur/rust`
    # cold-fetches rustc and its crates.
    "static.rust-lang.org",
    "index.crates.io",
    "static.crates.io",
    # Container images and OS packages
    "ghcr.io",
    "pkg-containers.githubusercontent.com",
    "snapshot.debian.org",
    # Forgejo's own public release/CDN hosts, not the in-cluster instance
    "code.forgejo.org",
    "data.forgejo.org",
)

# A write surface, deliberately outside the bucket: anonymous github.com is read-only, while
# the API is what makes gists, issues and pushes reachable. Folding it in would hand every
# build fence a way to publish at once.
GITHUB_API = hosts("api.github.com")

# Hosts serving the operator's own accounts. A prompt-injected agent holding these reads the
# operator's mail, finances and study data, so the group is named to keep
# `test_operator_data_reaches_only_haku_sandbox` honest as consumers are added.
OPERATOR_DATA = hosts("api.coinbase.com", "haku-mailbox.allegedly.works", "*.ankiweb.net")

# The operator's Google account (mail, calendar, tasks). No fence here reaches it: agents reach
# it only through services that hold the token for them (agentplane's egress proxy, google-mcp).
OPERATOR_GOOGLE = hosts("www.googleapis.com", "gmail.googleapis.com", "tasks.googleapis.com")

# Keyed by the policy name, so a failure names the thing to open.
OPERATOR_DATA_FENCE = "allow-haku-cloud-api-egress"
HAKU_OPENCLAW_FENCE = "allow-haku-openclaw-spike-proxy-egress"

# What each fence may connect to on the public internet.
ALLOWLISTS = {
    OPERATOR_DATA_FENCE: frozenset(host for group in egress_fences._HAKU_CLOUD_API_GROUPS for host in group),
    HAKU_OPENCLAW_FENCE: frozenset(egress_fences.OPENCLAW_SPIKE_ALLOWLIST),
    "allow-cloud-api-egress": frozenset(host for group in egress_fences._MITMPROXY_GROUPS for host in group),
}


def test_build_registries_are_all_or_none() -> None:
    """No fence carries part of the bucket.

    The hosts are grouped because they share a trust level, so a fence holding a subset is
    drift rather than a decision -- which is exactly how the fences diverged before they were
    pinned. GITHUB_GIT is separable from the package registries (a fence may read source
    without building), but neither bucket may be held in part.
    """
    for fence, allowed in ALLOWLISTS.items():
        for bucket in (GITHUB_GIT, BUILD_REGISTRIES - GITHUB_GIT):
            assert allowed & bucket in (frozenset(), bucket), fence


def test_operator_data_reaches_only_haku_sandbox() -> None:
    """The tier that reads the operator's own accounts stays in one fence.

    A coding agent that gained these would turn each of its injection surfaces into a path to
    the operator's mail and finances.
    """
    assert ALLOWLISTS[OPERATOR_DATA_FENCE] >= OPERATOR_DATA
    for fence, allowed in ALLOWLISTS.items():
        if fence != OPERATOR_DATA_FENCE:
            assert not (allowed & OPERATOR_DATA), fence


def test_no_fence_reaches_the_operators_google_account() -> None:
    for fence, allowed in ALLOWLISTS.items():
        assert not (allowed & OPERATOR_GOOGLE), fence


def test_github_api_reaches_only_declared_holders() -> None:
    """`api.github.com` is a write surface, so every grant is named explicitly."""
    holders = {fence for fence, allowed in ALLOWLISTS.items() if allowed & GITHUB_API}
    assert holders == {HAKU_OPENCLAW_FENCE}


if __name__ == "__main__":
    pytest_bazel.main()
