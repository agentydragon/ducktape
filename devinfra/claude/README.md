# Claude Code Integration

Session hooks, statusline, and Claude Code API models for Claude Code sessions. What
the Rust hook daemon guarantees (CLI and web): <claude_hook/SPEC.md>; this README
covers how.

## Networking

- **Transparent MITM (classic web containers)**: no `HTTPS_PROXY`; Anthropic's
  TLS-inspection CA is already in `/etc/ssl/certs/ca-certificates.crt`, so tools work
  as-is.
- **CCR agent proxy (Claude Code Remote / remote-execution sessions)**: `HTTPS_PROXY`
  is a local TLS-re-terminating relay (`http://127.0.0.1:46587`); tools must trust
  `/root/.ccr/ca-bundle.crt` (standard `*_CA_*` env vars and system store are
  pre-set; see `/root/.ccr/README.md`). **Gotcha:** the proxy's boot-time JVM
  truststore seed races `ca-certificates-java` and often fails, so `bb`/`bbr` can't
  verify the proxy (`bazelisk` is fine). `web_setup.sh` self-heals; root cause and
  detection: <docs/ccr_bazel_truststore_race.md>.

## Hook daemon

- **PATH shims** at `<session_dir>/bin/{bazelisk,bazel,bb,bbr}` run
  `claude-hook shim <name>`; the Rust shim runtime resolves the real binary outside
  the shim dir and never calls the daemon. `bazel`/`bazelisk` inject the session bazelrc and translate
  inherited `HTTP(S)_PROXY` into Java proxy JVM properties (grpc-java → BuildBuddy);
  `bb`/`bbr` only resolve the real binary. `claude-hook` is PATH-resolved at exec
  time, so `nix profile install` / `home-manager switch` takes effect without a
  session restart.
- **`git` shim**: installed only when the active profile enables a `git_shim` flag;
  per flag it blocks `git add -A`/`git add .`, `git stash`, or `git commit --amend`.
- Installs pre-commit git hooks and writes env vars to `CLAUDE_ENV_FILE`. Hook
  registration: `.claude/settings.json`. Connectivity probe: not implemented (see
  <TODO.md>). flux, kustomize, helm come from `@multitool//tools/*`, not the
  devshell.
- **Profile**: `DUCKTAPE_CLAUDE_HOOKS_PROFILE` = path to profile YAML; schema in
  <claude_hook/config.rs>.

### Gotcha: `Setup` and `SessionStart` carry different session IDs

After compaction (observed 2026-03-21), `Setup` fires with the **new** session ID and
`SessionStart` with the **old** one (`source: compact`). The daemon is keyed by
session ID (own socket and dir), so:

- Never register `claude-hook` for `Setup` — it would start a second daemon (its
  `Setup` handler is a no-op anyway). Plain scripts that don't call `claude-hook`
  are fine: `Setup` runs `bash devinfra/claude/web_setup_hook.sh` to reinstall
  devtools.
- Session-local files (socket, shim dir, session bazelrc) are keyed by
  `SessionStart`'s ID, which may be the old one.
- Shared statusline state lives in `~/.cache/claude-hooks` (honors
  `XDG_CACHE_HOME`).

### Files

- `<session_dir>` = `~/.claude/session-env/<session_id>/` (Claude Code-managed): shim
  dir, session `bazelrc`.
- `/tmp/claude-hd/<session_id>/`: `d.sock` (hook RPC), `daemon.pid`, `daemon.log`
  (incl. session start), `daemon.err.log`, `startup_failure.json` (client startup
  backoff marker).

## Claude Code Telemetry

From Claude Code's native exporter; the Rust daemon emits no OTLP (the profile `otel`
field is ignored).

- **Local machines**: <../../nix/home/claude_code/default.nix> exports directly to
  `https://alloy-otlp.allegedly.works`, bearer via an `otelHeadersHelper` reading the
  SOPS token; NixOS inline Home Manager hosts inherit it via
  <../../nix/home/home.nix>.
- **Web and Haku**: export to `http://127.0.0.1:4318`; <otlp_forwarder.py> (started
  by <ensure_otel_forwarder.sh>) attaches the bearer from
  `DUCKTAPE_OTEL_BEARER_TOKEN` or the mirrored `alloy-otlp-bearer` Secret
  (<../../cluster/k8s/agents/alloy-otlp-bearer/>) and forwards to Alloy.
- **Token**: Authentik `alloy-otlp-client-credentials` provider; the
  `authentik-jwt-rotation` CronJob commits a proxy-scoped JWT SOPS-encrypted to
  `secrets/alloy-otlp-bearer-token.yaml`, which `cli_env.sh`/`web_env.sh` decrypt and
  export. Until the job first succeeds the file is absent and setup warns.

Rationale and hosted-env variables: <plans/transcript_collection.md>. Archive past
cloud sessions with <session_export/README.md>.

## Web Setup

Configure both in the Claude Code web UI.

### 1. Environment variables (UI → Settings → Environment Variables)

| Variable                        | Value                                                                                                                |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE` | `devinfra/claude/claude_hook/profiles/web/profile.yaml` (`home-manager.yaml` sibling in `home-manager` install mode) |
| `SOPS_AGE_KEY`                  | Age private key (`AGE-SECRET-KEY-1...`)                                                                              |

Telemetry (forwarded by the session OTLP forwarder, above):

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

- These must be **UI env vars**: only those reach the `claude` process and hook
  subprocesses; startup-script exports reach Bash subprocesses only.
- **Gotcha: `..._TEMPORALITY_PREFERENCE=cumulative` is load-bearing.** Claude Code
  defaults to delta, which Alloy's `otelcol.exporter.prometheus` drops **silently**
  (no error or refused counter) while traces and logs still arrive:
  <../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>.
- Content knobs (`OTEL_LOG_*`) are on deliberately: ingestion is operator-only.

### 2. Setup script

```bash
bash ducktape/devinfra/claude/web_setup.sh
```

<web_setup.sh> (header lists its steps and modes) installs:

1. Nix + devtools (`claude-hook`, statusline, `bbapi`, `gh`, `sops`, skills).
2. Attic substituters `https://cache.allegedly.works/{public,main}` (pubkeys from
   <../../nix/attic-pubkeys.json>, `fallback = true`) in `/etc/nix/nix.custom.conf`
   — required in-session, where GitHub-release fixed-output fetches 403 through the
   proxy. `public` is anonymous and holds the web/Haku bootstrap closures, so it works
   before any credential exists (<../../cluster/docs/nix_cache.md> § Public bootstrap
   cache). `main` is private: `web_env.sh` upserts its per-principal reader JWT
   (rotated by `attic-jwt-rotation`, `cluster/cdk8s/nix_cache/attic.py`) into
   `/nix/var/determinate/netrc` at daemon startup.
3. `github-no-proxy` git remote + `buildbuddy.remote-bazel-remote-name` for `bbr`.
4. Skills symlinked into `~/.claude/skills/` (Anthropic defaults preserved).
5. `~/.bazelrc` with a shared `--disk_cache` (50 GiB cap) at `~/.cache/bazel/disk`
   for all worktrees (web only; CLI machines:
   <../docs/bazel_worktree_cache_sharing.md>). Shims don't pass `--nohome_rc`, so it
   applies.

**Usable disk is ~14 GiB, not ~235 GiB**: ~85% of root ext4 blocks are reserved and
unreclaimable from a session. Free space by deleting stale `.claude/worktrees/`:
<web_env/docs/container_spec.md> § Reserved blocks.

**Install mode** (`DUCKTAPE_WEB_SETUP_MODE` or `--mode=`):

| Mode                          | Devtools + skills                                                                                                                                                                                                                                                          |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile` (default)           | `nix profile install .#devtools` (`devToolPackages` + `localOnlyPackages`); skills linked into `~/.claude/skills/`; PATH via `/usr/local/bin`.                                                                                                                             |
| `home-manager` (experimental) | `home-manager switch --impure --flake .#claude-web` (<../../nix/home/hosts/claude-web.nix>): `devToolPackages` only, direnv + nix-direnv, skills via <../../nix/home/skills.nix>; PATH via `~/.nix-profile/bin`. Deploys no Claude Code settings, plugins, or MCP servers. |

The two hooks profiles (<claude_hook/profiles/web/profile.yaml>,
<claude_hook/profiles/web/home-manager.yaml>) are standalone copies kept in sync,
differing only in that PATH bridge.

**Flake output** (`profile` mode): `.#devtools`, or `DUCKTAPE_WEB_SETUP_OUTPUT`. Haku's
setup script (<../../haku/runtime/claude_web_env/README.md>) sets `agent-haku`, which
adds fastmcp (`fastmcp call <url> --auth <bearer>`), himalaya, and tea; it is also
the default when the hooks profile path contains `haku`. Haku's profile materializes
tea config from `haku-forgejo-tea`; other sessions can fetch `claude-forgejo-tea`
from `claude-sandbox`.

**Secrets**: `web_setup.sh` decrypts nothing — `SOPS_AGE_KEY` reaches only the
`claude` process. The hook daemon decrypts via `startup_env_script` (`web_env.sh`)
at startup: <docs/secrets_env_flow.md>.
