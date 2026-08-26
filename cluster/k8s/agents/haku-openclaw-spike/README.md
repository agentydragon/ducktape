# Haku OpenClaw spike

An isolated compatibility deployment at
<https://haku-openclaw-spike.allegedly.works> proving that OpenClaw can use
Claude Code as a persistent, subscription-backed runtime while retaining
OpenClaw sessions, workspace memory, and Haku Console's approval-gated MCP
tools.

## Image build

`haku/openclaw_spike/default.nix` builds the image entirely with Nix, the same
mechanism as the public-coder `openclaw` image. The OpenClaw gateway comes from
`nix-openclaw`, and Claude Code plus the spike's authoring/runtime tools are
layered from the repository's locked Nix package set — one controlled closure,
one Node.

Deviation: public-coder uses `nix-openclaw`'s stable gateway, but this spike
needs a newer beta line for its Claude model metadata, and `nix-openclaw` only
tracks stable. So it builds the gateway from a beta `sourceInfo` override
(currently `v2026.8.1-beta.3`) — nix-openclaw's supported source-override path.
Bump = update `betaSourceInfo` in the Nix file (tag/rev/hashes).

This replaced an earlier hybrid that pulled the upstream
`ghcr.io/openclaw/openclaw` beta as a Docker base and layered Nix tools on top.
That shipped two Node runtimes; the base image's Node linked an unsafe system
SQLite (3.51.2) that OpenClaw's WAL-safety guard rejects at startup, so the pod
crash-looped.

## Trust boundary

- The OpenClaw pod contains no real Claude OAuth token, GitHub PAT, Haku
  Forgejo password, or Haku Console bearer. It receives token-shaped
  placeholders only. The init
  container registers the Claude placeholder in OpenClaw's per-agent auth store
  because the `claude-cli` runtime intentionally strips inherited auth variables.
- `haku-openclaw-spike-proxy` in `haku-egress-proxy` holds the real values and
  substitutes them only in `Authorization` headers for their exact hosts.
- Namespace egress permits only DNS and that proxy. The proxy has a separate
  destination allowlist enforced by Cilium.
- The pod has no Kubernetes service-account token. Privileged or external work
  remains behind the ordinary Haku Console MCP approval boundary.

## TLS trust

The interception root reaches clients as the **system bundle** at
`/etc/ssl/certs/ca-certificates.crt` (mounted from `haku-egress-proxy-ca-cert`),
which covers everything that reads it — OpenSSL, `curl`, and GnuTLS/git. Three
runtimes bundle their own roots instead and are pointed at that file explicitly:

| Runtime                | How it is pointed at the bundle                   |
| ---------------------- | ------------------------------------------------- |
| Node                   | `NODE_EXTRA_CA_CERTS`                             |
| Bazel's JVM downloader | PKCS12 truststore planted by the init container   |
| Python / pip           | `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT` |

**Gotcha: a missing entry here presents as unreachability, not as a trust
error.** `pypi.org` and `files.pythonhosted.org` are both on the egress
allowlist, so before pip was pointed at the bundle its failures read as "no
route to PyPI" — a wrong diagnosis that reached committed guidance before it was
retested. Which CA variable each TLS backend actually honours was measured in
<../../../../plans/personal_agents/findings/egress_and_tls.md>.

## Persistent workspace

The 30 GiB PVC is mounted as `/home/openclaw`. It contains both:

- OpenClaw state and the agent workspace at
  `/home/openclaw/.openclaw/workspace`; and
- Claude Code's native transcripts/session metadata under
  `/home/openclaw/.claude`.

The deployment intentionally does **not** clone or reset a repository. The
first Haku session may reshape the workspace, initialize Git, or make it track a
new branch/remote of `haku/haku-state` using:

- `HAKU_STATE_REPO_URL` — the in-cluster Forgejo URL;
- `HAKU_GIT_USERNAME`; and
- `HAKU_GIT_PASSWORD` — a non-secret proxy placeholder.

A placeholder-only `.netrc` and Git author identity are planted so ordinary
`git clone`, fetch, and push use the mediated Forgejo credential. The same
`.netrc` contains the non-secret GitHub placeholder, while `GH_PAT` supports
explicit GitHub API authentication. The proxy replaces either form with the
`agentydragon-agent` PAT only for exact GitHub hosts. Repository layout and
branch policy remain agent/operator decisions rather than GitOps bootstrap.

## Scope

This is a spike, not a migration of Haku Console's existing Claude chat route.
Success means:

1. Claude Code answers through OpenClaw using subscription OAuth.
2. Follow-up turns reuse one live Claude process and survive process restart by
   session resume.
3. OpenClaw local tools and Haku Console MCP tools work from Claude.
4. `MEMORY.md` and `memory/*.md` survive and are retrievable.
5. Haku can initialize and push its workspace repository without ever seeing
   the real Forgejo credential.
