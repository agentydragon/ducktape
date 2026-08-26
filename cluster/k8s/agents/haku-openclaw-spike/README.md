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

## Version constraints

Why the gateway version and Node are pinned the way they are:

- **Opus 5 metadata is beta-only.** OpenClaw's `anthropic` extension lists
  `claude-opus-5` (1M context) in its model catalog only from the
  `2026.7.2`/`2026.8.x` **beta** line; stable `2026.7.1-2` tops out at
  `claude-opus-4-8`. The `claude-cli` runtime still _runs_ any model via the
  bundled `claude` binary — the catalog is only metadata (picker label,
  context-window display/limits).
- **The beta fixes Tool Search core-tool visibility.** Stable `2026.7.1-2` can
  drop core tools (`exec`/`process`) from a turn when Tool Search is active
  (openclaw#126460); the fix — "keep core coding tools visible when tool search
  is enabled", commit `65d58c4`, first in `v2026.7.2-beta.5` — plus the
  synthetic-runtime-turn fix (`7fcdded`, `v2026.8.1-beta.2`) are both ancestors
  of the pinned `v2026.8.1-beta.3`. (Upstream Tool Search repair is not fully
  settled: openclaw#126618 is still open.)
- **The beta refuses WAL-unsafe SQLite.** From `2026.7.2-beta` on, the gateway
  aborts at startup unless Node's SQLite is WAL-safe (rejects `3.51.0`–`3.51.2`;
  needs `≥3.51.3`, or `3.50.7+`/`3.44.6+` in those series). Stable `2026.7.1-2`
  has no such check and runs on the unsafe SQLite silently — as public-coder-agent
  currently does.
- **Node / SQLite in nixpkgs.** `nodejs_22` = `22.23.2` → SQLite `3.51.2`
  (unsafe); `nodejs_24` = `24.19.0` → SQLite `3.53.3` (safe). OpenClaw's
  `engines.node` allows `>=22.22.3 <23`, `>=24.15.0 <25`, or `>=25.9.0`, so Node
  24 is supported. nix-openclaw hardcodes `nodejs_22`, so the build overrides it
  to a WAL-safe Node.
- **pnpm: the offline reader must match the store writer.** The beta's
  `pnpm-lock.yaml` is authored by pnpm `11.15.1` (lockfileVersion `9.0`), and the
  from-source build sees two different pnpm versions across its two stages. The
  dependency-fetch FOD leaves pnpm's `manage-package-manager-versions` on, so
  pnpm self-switches to the source's `packageManager` (`11.15.1`) and writes the
  store in that format — regardless of the `11.2.2` nix-openclaw pins. The
  gateway's offline `pnpm install` then runs _after_ `gateway-postpatch` strips
  the `packageManager` field, so it uses nix-openclaw's pinned `11.2.2` directly;
  that reader can't resolve a normally-locked dependency (`@clack/core`) out of
  the `11.15.1`-written store and aborts with `ERR_PNPM_NO_OFFLINE_TARBALL`. The
  build splices pnpm `11.15.1` into a copy of the nix-openclaw tree so the offline
  reader matches the writer. This bites only the from-source (beta) path;
  public-coder consumes the prebuilt stable gateway and runs no pnpm. Bumping the
  beta may mean re-pinning this pnpm to whatever the new lock was authored with.
- **The nix-store plugin-ownership patch is stable-only — and unneeded here.**
  nix-openclaw's `allow-nix-store-plugin-ownership.patch` (lets the gateway trust
  its read-only nix-store plugins) applies cleanly to stable but rejects hunks on
  every beta — because `2026.8.1-beta.3` already carries the same behavior
  upstream (`discovery.ts` trusts `IMMUTABLE_NIX_STORE` roots; `hardlink-policy.ts`
  trusts nix-store roots in nix mode). So the build sets
  `applyNixStorePluginOwnershipPatch = false` rather than porting the patch.

TODO: upstream nix-openclaw changes so the gateway Node and (for from-source
builds) pnpm are bumped or configurable, then drop the local `nodejs_24` and
pnpm `11.15.1` overrides. The Node hardcode affects the public-coder image too;
the pnpm pin only bites the from-source beta build.

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
