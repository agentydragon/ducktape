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
needs a newer beta line (see [Version constraints](#version-constraints)), and
`nix-openclaw` only tracks stable. `openclaw@2026.8.1-beta.3` is published on
npm, so the build points `nix-openclaw`'s own npm-package gateway build at the
beta: a beta-pinned wrapper lock (`haku/openclaw_spike/npm_wrapper/`) is spliced
over `nix-openclaw`'s stable one (`nix/npm/openclaw/`), driving the identical
`buildNpmPackage` path. Bump = regenerate `npm_wrapper/` (`npm install
openclaw@<ver> --package-lock-only --omit=dev`), update `betaSourceInfo`
(`releaseVersion`), and refresh `gatewayNpmDepsHash`.

Use the npm-package path, **not** a from-source `sourceInfo` override:
`nix-openclaw`'s own stable is npm-package too, so its from-source pnpm build is
unexercised and is missing fetcherVersion-4 store steps (`index.db`
reconstruction), which makes the gateway's offline install fail
(`ERR_PNPM_NO_OFFLINE_TARBALL`).

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
- **Nix-store plugin trust is patched into the npm dist.** nix-openclaw's
  `patch-openclaw-npm-dist.mjs` rewrites the bundled ownership/hardlink checks so
  the gateway trusts its read-only nix-store plugins. It is idempotent and
  applies cleanly to the beta dist. A future beta whose bundled dist diverges
  could break this patch — a build-time `fail`, not a silent skip.

TODO: upstream a nix-openclaw change to bump the gateway Node (or make it
configurable), then drop the local `nodejs_24` override — the Node hardcode
affects the public-coder image too.

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
