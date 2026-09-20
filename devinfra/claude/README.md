# Claude Code Integration

Session hooks, statusline, and Claude Code API models for Claude Code
web environments.

## Networking

Two egress models exist across Claude Code environments:

- **Transparent MITM (classic web containers).** No `HTTPS_PROXY` is set and Anthropic's
  TLS-inspection CA is already in the system bundle (`/etc/ssl/certs/ca-certificates.crt`),
  so curl, Bazel, pip, npm, kubectl, git, etc. work out of the box. The hook daemon doesn't
  distinguish network-layer MITM from direct egress; outbound HTTPS to known hosts reaches
  them, end of story.
- **CCR agent proxy (Claude Code Remote / remote-execution sessions).** `HTTPS_PROXY`
  points at a local relay (`http://127.0.0.1:46587`) that re-terminates TLS with its own
  CA; every tool must trust `/root/.ccr/ca-bundle.crt` (the standard `*_CA_*` env vars and
  system store are pre-set). See `/root/.ccr/README.md`. **Gotcha:** the proxy's boot-time
  JVM-truststore seed races `ca-certificates-java` and often fails, so `bb`/`bbr` can't
  verify the proxy's TLS (`bazelisk` is fine). `web_setup.sh` self-heals this; root cause +
  detection in <docs/ccr_bazel_truststore_race.md>.

## Specification

See <claude_hook/SPEC.md> for the high-level, user-facing specification of
what the Rust hook daemon guarantees to every Claude Code session (on CLI and on
web). Read that first if you want to know **what** the daemon does for the
agent — this README covers **how** those behaviors are implemented.

## Session Start Hook

The hook runs at the start of each Claude Code web session and:

### Connectivity Probe

Not currently implemented in Rust. See <TODO.md> for the remaining
connectivity-probe follow-up.

### PATH Shims (self-contained Rust runtime)

Installs PATH shims at `<session_dir>/bin/{bazelisk,bazel,bb,bbr}` — small
shell scripts that `exec claude-hook shim <name>` (PATH-resolved at invocation
time). The Rust shim runtime resolves the real binary outside the shim dir
and never calls back into the session-start daemon.

`bazel` and `bazelisk` inject the session bazelrc and translate any inherited
`HTTP_PROXY` / `HTTPS_PROXY` value into Java proxy JVM properties so Bazel's
grpc-java clients can reach BuildBuddy through Claude's proxy. `bb` and `bbr`
are real-binary resolution wrappers only.

The `git` shim is installed only when the active profile enables at least one
`git_shim` safety flag. Its per-flag policy can block `git add -A` / `git add .`,
`git stash`, or `git commit --amend`.

Because `claude-hook` is resolved via PATH at exec time (not baked as a store
path at install time), `nix profile install` / `home-manager switch` takes
effect for all subsequent shim invocations without restarting the session.

### Git Hooks and Environment

Installs git pre-commit hooks (pre-commit framework) and writes environment
variables to `CLAUDE_ENV_FILE`. Bazelisk comes from the Nix devShell; flux,
kustomize, helm are Bazel-managed via `@multitool//tools/*`; Nix formatting
uses `nixfmt` from the devShell. See `.claude/settings.json` for hook
configuration.

## Observed: `Setup` and `SessionStart` Use Different Session IDs

**Observed 2026-03-21 during session compaction.** Claude Code sends hook events with
_mismatched_ session IDs: the `Setup` hook fires with the **new** post-compaction session
ID, while the `SessionStart` hook fires with the **old** pre-compaction session ID (with
`source: compact`).

Example (from daemon traces):

| Hook           | Session ID                                   |
| -------------- | -------------------------------------------- |
| `Setup`        | `f1126fbf-c415-48e0-8b16-09b95c4b556a` (new) |
| `SessionStart` | `c11a6aa8-4bb3-4bfb-8d25-3224a2ab7efb` (old) |

**Why this matters — session-local vs session-global state:**

The hook daemon is keyed by session ID: each session ID gets its own socket path and daemon
directory. When `Setup` starts a daemon for the new ID, and `SessionStart` arrives for the
old ID, the client finds no socket for the old ID and tries to start a _second_ daemon.

**Consequences:**

- **`Setup` hook with `claude-hook`**: Do NOT register `claude-hook` for the `Setup`
  event. It would start a daemon for the new session ID. The `Setup` hook handler is a
  noop anyway (the daemon returns `{}` immediately).
- **`Setup` hook with plain shell scripts**: Safe, as long as the script does NOT call
  `claude-hook`. We register `bash devinfra/claude/web_setup_hook.sh` for Setup — it
  reinstalls devtools before SessionStart fires, but never invokes `claude-hook`.
- **Session-local files** (socket, shim dir, session bazelrc): always keyed by
  `SessionStart`'s session ID, which may be the _old_ ID after a compaction.
- **Shared statusline state** uses `~/.cache/claude-hooks` by default and
  respects `XDG_CACHE_HOME`. This path remains stable across the `claude-hook`
  and `claude-statusline` package renames. Bazelisk is not cached there: its
  per-session shim lives under `<session_dir>/bin/` and resolves the real
  executable from the Nix devtools `PATH` at invocation time.

## Configuration

| Environment Variable            | Default | Description          |
| ------------------------------- | ------- | -------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE` | (none)  | Path to profile YAML |

`<session_dir>` = `~/.claude/session-env/<session_id>/` — a per-session directory managed by Claude Code.

The Rust daemon's profile schema is defined in <claude_hook/config.rs>.

## References

- [Claude Code Hooks API](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Settings JSON Schema](https://json.schemastore.org/claude-code-settings.json)
- [Claude Code on the Web](https://www.anthropic.com/news/claude-code-on-the-web) - Product announcement
- [Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) - Network isolation architecture
- [Enterprise Network Configuration](https://docs.anthropic.com/en/docs/claude-code/corporate-proxy) - Proxy and CA configuration
- [Network Security](https://docs.anthropic.com/en/docs/claude-code/security#network-access) - Egress controls

## Files

Agent shell files live under `<session_dir>` = `~/.claude/session-env/<session_id>/`.
The Rust daemon does not set up supervisor or Docker.

Rust hook daemon files (in `/tmp/claude-hd/<session_id>/`):

- `d.sock` - UDS for hook RPC
- `daemon.pid` - Daemon pidfile
- `daemon.log` - Daemon and session start logs
- `daemon.err.log` - Daemon stderr
- `startup_failure.json` - client startup backoff marker

## Historical Context

Pre-Firecracker networking and supervisor workarounds are preserved in git
history. Current code and docs assume Firecracker sessions.

## Claude Code Telemetry

The Rust `claude-hook` daemon does not emit OTLP spans. It writes local daemon
logs under `/tmp/claude-hd/<session_id>/`; the legacy profile `otel` field is
ignored. The telemetry described here comes from Claude Code's native exporter.

On local machines, the Home Manager Claude Code module
<../../nix/home/claude_code/default.nix> points that exporter directly at
`https://alloy-otlp.allegedly.works` and supplies headers with an
`otelHeadersHelper` that reads the SOPS-managed bearer token. NixOS inline Home
Manager hosts inherit this through <../../nix/home/home.nix>.

Web and Haku sessions send to the local relay at
`http://127.0.0.1:4318`. <otlp_forwarder.py>, started by
<ensure_otel_forwarder.sh>, attaches the bearer from
`DUCKTAPE_OTEL_BEARER_TOKEN` or the mirrored `alloy-otlp-bearer` Secret
<../../cluster/k8s/agents/alloy-otlp-bearer/> and forwards telemetry to Alloy.

Authentik is the token source. The Terraform module creates a dedicated
`alloy-otlp-client-credentials` OAuth2 provider; the shared
`authentik-jwt-rotation` CronJob exchanges its source JWT for a proxy-scoped
JWT and commits the result SOPS-encrypted to
`secrets/alloy-otlp-bearer-token.yaml`. `cli_env.sh` and `web_env.sh` decrypt
that file and export the bearer. On first deployment, the file is absent until
the rotation job succeeds, and environment setup warns that telemetry auth is
not available yet.

Rationale, probe evidence, and the hosted-environment variables are in
<plans/transcript_collection.md>.

## Web Setup

To use this repository with Claude Code on the web, configure **both** of the following in the Claude Code web UI:

### 1. Environment Variables (Claude Code web UI → Settings → Environment Variables)

These must be configured as env vars in the Claude Code web UI so they are injected into the Claude process at startup:

| Variable                        | Description                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE` | Path to the profile: `devinfra/claude/claude_hook/profiles/web/profile.yaml` |
| `SOPS_AGE_KEY`                  | Age private key for SOPS decryption (format: `AGE-SECRET-KEY-1...`)          |

`DUCKTAPE_CLAUDE_HOOKS_PROFILE` is needed so Claude Code injects the profile path into all hook subprocesses.
`SOPS_AGE_KEY` is the age private key for decrypting secrets. The hook daemon receives it from the Claude process environment via `startup_env_script`.

**Claude Code native telemetry** (Grafana dashboards via the session OTLP
forwarder; see the Claude Code Telemetry section above and
<plans/transcript_collection.md> for rationale):

```text
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_TRACES_EXPORTER=otlp
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
OTEL_LOG_USER_PROMPTS=1
OTEL_LOG_TOOL_DETAILS=1
OTEL_LOG_TOOL_CONTENT=1
OTEL_LOG_RAW_API_BODIES=1
```

**Gotcha: `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` is load-bearing.**
Claude Code defaults to **delta** temporality, which Prometheus/Mimir cannot ingest.
Alloy's `otelcol.exporter.prometheus` drops delta metrics **silently** — no error, no
counter, nothing in `otelcol_receiver_refused_*` — so metrics disappear between the OTLP
receiver and `remote_write` while traces and logs arrive normally. Omit this var and the
symptom is "telemetry doesn't work" with a completely healthy-looking pipeline. See
<../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>.

Content-inclusion knobs (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`,
`OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_RAW_API_BODIES`) are enabled for full logging
to operator-only ingestion. These must be **UI env vars**: only that mechanism
reaches the claude process (startup-script exports reach Bash subprocesses only).

### 2. Setup Script

```bash
bash ducktape/devinfra/claude/web_setup.sh
```

This runs <web_setup.sh> which installs:

1. Nix + devtools (`claude-hook` Rust binary, Python statusline, `bbapi`, `gh`, `sops`, skills)
2. Attic substituter config: `extra-substituters = https://cache.allegedly.works/public
https://cache.allegedly.works/main` (+ trusted pubkeys from
   <../../nix/attic-pubkeys.json> and `fallback = true`) in `/etc/nix/nix.custom.conf`,
   so tool closures substitute instead of building from source — required in-session,
   where GitHub-release fixed-output fetches (e.g. bazel-diff's deploy jar) 403 through
   the proxy. `public` is anonymous-readable and carries exactly the web/Haku bootstrap
   closures, so it substitutes even on the very first install of a fresh rootfs, before
   any session credential exists (see <../../cluster/docs/nix_cache.md> "Public
   bootstrap cache"). `main` is private — its per-principal reader JWT (rotated by
   `cluster/k8s/nix-cache/cronjob.yaml`) is upserted into
   `/nix/var/determinate/netrc` by `web_env.sh` at hook-daemon startup, so it stays
   anonymous (and unused, since `fallback = true` already got everything from `public`)
   until then; every later `nix` invocation substitutes from it too, with auth.
3. `github-no-proxy` git remote + `buildbuddy.remote-bazel-remote-name` for bbr
4. Skills symlinked into `~/.claude/skills/` (preserves Anthropic defaults)
5. A user-level `~/.bazelrc` with a shared local `--disk_cache` (50 GiB GC cap) at
   `~/.cache/bazel/disk`, so all Bazel server instances and worktrees in the container
   reuse locally-executed action results across the persistent rootfs. Web sessions
   only — CLI machines configure their own (<../docs/bazel_worktree_cache_sharing.md>).
   The shims inject the session bazelrc without `--nohome_rc`, so Bazel still reads this
   home rc.

**Usable disk is ~14 GiB, not ~235 GiB.** The root ext4 reserves ~85% of its blocks for
`nobody:nogroup` and a session cannot reclaim them: the `devices:` cgroup denies opening
`/dev/vda` even to root, so `tune2fs` cannot run, and `resv_strict` makes the mount-option
route ineffective. Reclaim space by deleting — usually stale agent worktrees under
`.claude/worktrees/`. Measurements and both dead workarounds:
<web_env/docs/container_spec.md> § Reserved blocks.

#### Install mode

`web_setup.sh` supports two install modes, selected by the `DUCKTAPE_WEB_SETUP_MODE`
env var (or a `--mode=<...>` arg):

| Mode                          | How devtools + skills are installed                                                                                                                                                                                                    |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile` (default)           | `nix profile install .#devtools` installs `devToolPackages` plus `localOnlyPackages`; skills are linked into `~/.claude/skills/`.                                                                                                      |
| `home-manager` (experimental) | `home-manager switch --impure --flake .#claude-web` installs `devToolPackages` (omitting `localOnlyPackages`) and deploys skills through the shared HM skills module (<../../nix/home/skills.nix>).                                  |

The `home-manager` mode activates `homeConfigurations.claude-web` (defined in `flake.nix`,
config at <../../nix/home/hosts/claude-web.nix>) — a standalone, minimal profile that
reuses the shared `devToolPackages` core from
[`nix/flake/devtools.nix`](../../nix/flake/devtools.nix); unlike the `.#devtools` profile,
it omits `localOnlyPackages`. It also adds direnv + nix-direnv and the shared skills module.
It deliberately does **not** import the
full home-manager host config, so — unlike the NixOS hosts — it deploys no Claude Code
settings, plugins, or MCP servers.

Each mode pairs with its own hooks profile (`DUCKTAPE_CLAUDE_HOOKS_PROFILE`); they are
standalone copies kept in sync, differing only in how the Nix devtools reach the agent's
`PATH`: <claude_hook/profiles/web/profile.yaml> (`profile` mode, via the `/usr/local/bin`
symlink bridge) vs. <claude_hook/profiles/web/home-manager.yaml> (`home-manager` mode, via
`~/.nix-profile/bin` directly). In `home-manager` mode, point `DUCKTAPE_CLAUDE_HOOKS_PROFILE`
at the `home-manager.yaml` sibling.

**Which flake output** (orthogonal to install mode): in `profile` mode
`web_setup.sh` installs `.#devtools` by default, or the output named by
`DUCKTAPE_WEB_SETUP_OUTPUT`. Haku's <../../haku/runtime/claude_web_env/setup.sh> sets
`DUCKTAPE_WEB_SETUP_OUTPUT=agent-haku` to get `.#agent-haku`, which composes
`.#devtools` and adds Haku-only CLIs: fastmcp (`fastmcp call <url> --auth
<bearer>`) for in-cluster MCP facades, himalaya for mailbox access, and tea for
Gitea/Forgejo workflows. Haku's profile materializes tea config from
`haku-forgejo-tea`; generic Claude sessions can fetch `claude-forgejo-tea` from
`claude-sandbox` when they need a `tea` login. Claude web uses the lean default.

Secrets are **not** decrypted by `web_setup.sh`. `SOPS_AGE_KEY` is a user UI env var
delivered only to the interactive Claude Code process — not to the setup script. All
decryption happens in the hook daemon via `startup_env_script` (`web_env.sh`) at
daemon startup, once `SOPS_AGE_KEY` is available in the inherited env.

See <docs/secrets_env_flow.md> for the full picture.
