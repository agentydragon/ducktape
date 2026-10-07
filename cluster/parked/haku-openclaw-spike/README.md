# Haku OpenClaw spike (decommissioned)

## Retirement and revival

This directory is an archive, not a live Flux source. Generators live under
`cluster/cdk8s/parked/haku_openclaw_spike_*.py` and still render these snapshots. The Nix image source remains in
`haku/openclaw_spike/`; its disabled CI workflow is archived there as `image-workflow.yaml.disabled`. The old Authentik
blueprint is `haku-openclaw-spike-sso.yaml.disabled`: it is a revival input, not a Kubernetes manifest or an active
Authentik blueprint.

Retirement first pointed the existing `haku-openclaw-spike-app` Flux object at an empty GitRepository directory: its old
`deletionPolicy: Orphan` would otherwise have left the app running. Live checks on 2026-10-02 at 20:03 UTC confirmed
Flux Ready on the retirement merge, an empty inventory, and no spike namespace, PVCs, backup Flux owner, or dedicated
proxy Deployment. The empty retirement owner and its directory are now removed from GitOps, along with the temporary
SeaweedFS tenant grant. The archived generators still render, but do not deploy anything.

PVC data loss was intentional. Retained PVs or physical backing storage must be checked separately; namespace/PVC
deletion does not prove those were erased.

The backup bucket uses `Retain`: historical Restic data is not intentionally deleted. The repository password and
published token are archived as unchanged SOPS ciphertext; do not reuse the old token when reviving the app.

Revival requires deliberately restoring the Flux/artifact wiring, image workflow and automation, Authentik
provider/outpost/route (removing the retirement blueprint), namespace credential-store admission, SeaweedFS tenant
grant, shared-CA distribution, GitHub credential grant, and token rotation. The archived proxy expects the shared Haku
proxy namespace, CA, OAuth and Forgejo credentials to exist. Do not apply this archive as-is and assume that its old
credential references are still valid.

## Historical deployment notes

An isolated compatibility deployment at <https://haku-openclaw-spike.allegedly.works> proving that OpenClaw can use
Claude Code as a persistent, subscription-backed runtime while retaining OpenClaw sessions, workspace memory, and Haku
Console's approval-gated MCP tools.

## Image build

`haku/openclaw_spike/default.nix` builds the image entirely with Nix, using the same `dockerTools.buildLayeredImage`
approach as public-coder. The gateway is packaged through nix-openclaw's tested npm-package path using its pinned
OpenClaw wrapper and lockfile. Claude Code and the spike's tools are layered from the locked Nix package set.

The gateway release follows the nix-openclaw input pinned in `flake.lock`. Deployment image tags remain Flux-managed:
they advance only after the corresponding image-publish workflow runs on `devel`. Both images use the package and source
metadata pinned by nix-openclaw. Release-specific local dist repairs live in `openclaw/patches/` and are shared through
`openclaw/gateway.nix`.

Keep using nix-openclaw's npm-package path. Its separate from-source pnpm build is not the path validated for this image
and lacks the offline store materialization needed by the gateway build.

This replaced an earlier hybrid that used the upstream `ghcr.io/openclaw/openclaw` image as a Docker base and layered
Nix tools on top. The current image owns its Node runtime and does not depend on a second base-image Node.

## Version and runtime constraints

Both images follow the OpenClaw release pinned by nix-openclaw's locked revision. The shared gateway derives the
matching release-specific dist patch from that metadata; a missing patch fails the build and requires review.

- Keep the image and gateway on one Node runtime. Haku takes its Node package from the same Nix package set as the
  gateway. The WAL-reset SQLite runtime guard applies to Bun, not Node.

## Trust boundary

- The OpenClaw pod contains no real Claude OAuth token, GitHub PAT, Haku Forgejo password, or Haku Console bearer. It
  receives token-shaped placeholders only. The init container registers the Claude placeholder in OpenClaw's per-agent
  auth store because the `claude-cli` runtime intentionally strips inherited auth variables.
- `haku-openclaw-spike-proxy` in `haku-egress-proxy` holds the real values and substitutes them only in `Authorization`
  headers for their exact hosts.
- Namespace egress permits only DNS and that proxy. The proxy has a separate destination allowlist enforced by Cilium.
- The pod has no Kubernetes service-account token. Privileged or external work remains behind the ordinary Haku Console
  MCP approval boundary.

## TLS trust

The interception root reaches clients as the **system bundle** at `/etc/ssl/certs/ca-certificates.crt` (mounted from
`haku-egress-proxy-ca-cert`), which covers everything that reads it — OpenSSL, `curl`, and GnuTLS/git. Three runtimes
bundle their own roots instead and are pointed at that file explicitly:

| Runtime                | How it is pointed at the bundle                   |
| ---------------------- | ------------------------------------------------- |
| Node                   | `NODE_EXTRA_CA_CERTS`                             |
| Bazel's JVM downloader | PKCS12 truststore planted by the init container   |
| Python / pip           | `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT` |

**Gotcha: a missing entry here presents as unreachability, not as a trust error.** `pypi.org` and
`files.pythonhosted.org` are both on the egress allowlist, so before pip was pointed at the bundle its failures read as
"no route to PyPI" — a wrong diagnosis that reached committed guidance before it was retested. Which CA variable each
TLS backend actually honours was measured in <../../../../docs/personal_agents/findings/egress_and_tls.md>.

## Persistent workspace

The Deployment mounts the current 40 GiB-requested `state-v2` PVC at `/home/openclaw` (the local-path request is
capacity intent, not enforcement). It contains both:

- OpenClaw state and the agent workspace at `/home/openclaw/.openclaw/workspace`; and
- Claude Code's native transcripts/session metadata under `/home/openclaw/.claude`.

The deployment intentionally does **not** clone or reset a repository. The first Haku session may reshape the workspace,
initialize Git, or make it track a new branch/remote of `haku/haku-state` using:

- `HAKU_STATE_REPO_URL` — the in-cluster Forgejo URL;
- `HAKU_GIT_USERNAME`; and
- `HAKU_GIT_PASSWORD` — a non-secret proxy placeholder.

A placeholder-only `.netrc` and Git author identity are planted so ordinary `git clone`, fetch, and push use the
mediated Forgejo credential. The same `.netrc` contains the non-secret GitHub placeholder, while `GH_PAT` supports
explicit GitHub API authentication. The proxy replaces either form with the `agentydragon-agent` PAT only for exact
GitHub hosts. Repository layout and branch policy remain agent/operator decisions rather than GitOps bootstrap.

## Scope

This is a spike, not a migration of Haku Console's existing Claude chat route. Success means:

1. Claude Code answers through OpenClaw using subscription OAuth.
2. Follow-up turns reuse one live Claude process and survive process restart by session resume.
3. OpenClaw local tools and Haku Console MCP tools work from Claude.
4. `MEMORY.md` and `memory/*.md` survive and are retrievable.
5. Haku can initialize and push its workspace repository without ever seeing the real Forgejo credential.
