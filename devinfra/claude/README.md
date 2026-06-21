# Claude Code Integration

Session hooks, statusline, and Claude Code API models for Claude Code
web environments.

## Networking

Current Claude Code web containers reach the internet without any per-tool
proxy configuration: no `HTTPS_PROXY` env vars are set, and Anthropic's TLS
inspection CA is already in the system CA bundle
(`/etc/ssl/certs/ca-certificates.crt`) — so curl, Bazel, pip, npm, kubectl,
git, etc. all work out of the box. We don't try to distinguish whether that
works via a transparent network-layer MITM proxy or direct egress; as far
as the Rust hook daemon is concerned, outbound HTTPS to known hosts reaches
them, end of story.

## Specification

See <claude_hook/SPEC.md> for the high-level, user-facing specification of
what the Rust hook daemon guarantees to every Claude Code session (on CLI and on
web). Read that first if you want to know **what** the daemon does for the
agent — this README covers **how** those behaviors are implemented.

## Session Start Hook

The hook runs at the start of each Claude Code web session and:

### Connectivity Probe

Not currently implemented in Rust. See <TODO.md> for the Python parity
follow-up.

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
- **Session-global files** (bazelisk binary at `~/.cache/claude-hooks/bazelisk`): shared
  across all session IDs, safe for concurrent daemons.

## Configuration

| Environment Variable                    | Default | Description          |
| --------------------------------------- | ------- | -------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT` | `19001` | Supervisor TCP port  |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE`         | (none)  | Path to profile YAML |

`<session_dir>` = `~/.claude/session-env/<session_id>/` — a per-session directory managed by Claude Code.

See `settings.py` for the full configuration schema.

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

The explicit egress proxy era (`auth_proxy/` subsystem) and the gVisor/9p origin of the
TCP supervisor port: <archive/2026_06_12_gvisor_era_networking.md>.

## OTEL Tracing

Hooks emit OpenTelemetry traces to Grafana Alloy via Authentik proxy at
`alloy-otlp.allegedly.works`. Authentik is the canonical source for the bearer
JWT: the TF module creates a dedicated `alloy-otlp-client-credentials` OAuth2
provider, and the shared `authentik-jwt-rotation` CronJob mints a source JWT hourly
when the existing token has <24h of validity remaining. The job immediately
exchanges that source JWT into an `alloy-otlp` proxy-scoped JWT before writing
it to git, because Authentik proxy outposts only accept Bearers issued by the
proxy provider they introspect against. The job commits the final token
SOPS-encrypted to `secrets/alloy-otlp-bearer-token.yaml`; `cli_env.sh` and
`web_env.sh` decrypt that file and export it as `DUCKTAPE_OTEL_BEARER_TOKEN`.
On first deploy there is an expected bootstrap window: until the CronJob runs
once successfully, the file does not exist yet and env setup logs a warning
instead of exporting the OTEL token.

Configured in the profile path (`otel.endpoint`, `secrets.otel_bearer_token`).

Key files: TF module in <tf/gitops/alloy-otlp-bearer-token/> and the shared
rotator in <cluster/k8s/agents/authentik-jwt-rotation/>. Rotation is normally
automatic; to force a refresh, delete `secrets/alloy-otlp-bearer-token.yaml`
from `devel` or manually run the `authentik-jwt-rotation` CronJob.

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

### 2. Setup Script

```bash
bash ducktape/devinfra/claude/web_setup.sh
```

This runs <web_setup.sh> which installs:

1. Nix + devtools (`claude-hook` Rust binary, Python statusline, `bbapi`, `gh`, `sops`, skills)
2. `github-no-proxy` git remote + `buildbuddy.remote-bazel-remote-name` for bbr
3. Skills symlinked into `~/.claude/skills/` (preserves Anthropic defaults)
4. A user-level `~/.bazelrc` with a shared local `--disk_cache` (50 GiB GC cap) at
   `~/.cache/bazel/disk`, so all Bazel server instances and worktrees in the container
   reuse locally-executed action results across the persistent rootfs. Web sessions
   only — CLI machines configure their own (<../docs/bazel_worktree_cache_sharing.md>).
   The shims inject the session bazelrc without `--nohome_rc`, so Bazel still reads this
   home rc.

It also reclaims ~90% of the root ext4's `nobody:nogroup`-reserved blocks (the container
ships with ~84% reserved; we run as root) via `tune2fs -r`, freeing most of the 256 GiB
disk that is otherwise inaccessible — idempotent and skipped once the reservation is low.
See <web_env/docs/container_spec.md>.

#### Install mode

`web_setup.sh` supports two install modes, selected by the `DUCKTAPE_WEB_SETUP_MODE`
env var (or a `--mode=<...>` arg):

| Mode                          | How devtools + skills are installed                                                                                                                                                |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile` (default)           | `nix profile install .#devtools`, then per-skill symlinks into `~/.claude/skills/`.                                                                                                |
| `home-manager` (experimental) | `home-manager switch --impure --flake .#claude-web`. Home Manager installs the same devtools and deploys skills through the shared HM skills module (<../../nix/home/skills.nix>). |

The `home-manager` mode activates `homeConfigurations.claude-web` (defined in `flake.nix`,
config at <../../nix/home/hosts/claude-web.nix>) — a standalone, minimal profile that
reuses the flake's `devToolPackages` list (so the two modes can't drift) and adds
direnv + nix-direnv plus the shared skills module. It deliberately does **not** import the
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
`.#devtools` and adds the fastmcp MCP-client CLI (`fastmcp call <url> --auth
<bearer>`) for talking to in-cluster MCP facades; Claude web uses the lean default.

Secrets are **not** decrypted by `web_setup.sh`. `SOPS_AGE_KEY` is a user UI env var
delivered only to the interactive Claude Code process — not to the setup script. All
decryption happens in the hook daemon via `startup_env_script` (`web_env.sh`) at
daemon startup, once `SOPS_AGE_KEY` is available in the inherited env.

See <docs/secrets_env_flow.md> for the full picture.
