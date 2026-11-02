# Web Setup

Configure both in the Claude Code web UI.

## 1. Environment variables (UI → Settings → Environment Variables)

| Variable                        | Value                                                                                                                |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE` | `devinfra/claude/claude_hook/profiles/web/profile.yaml` (`home-manager.yaml` sibling in `home-manager` install mode) |
| `SOPS_AGE_KEY`                  | Age private key (`AGE-SECRET-KEY-1...`)                                                                              |

Telemetry (forwarded by the session OTLP forwarder described in [Claude Code Telemetry](../README.md#claude-code-telemetry)):

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
  <../../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>.
- Content knobs (`OTEL_LOG_*`) are on deliberately: ingestion is operator-only.

## 2. Setup script

```bash
bash ducktape/devinfra/claude/web_setup.sh
```

<../web_setup.sh> (header lists its steps and modes) installs:

1. Nix + devtools (`claude-hook`, statusline, `bbapi`, `gh`, `sops`, skills).
2. Attic substituters `https://cache.allegedly.works/{public,main}` (pubkeys from
   <../../../nix/attic-pubkeys.json>, `fallback = true`) in `/etc/nix/nix.custom.conf`
   — required in-session, where GitHub-release fixed-output fetches 403 through the
   proxy. `public` is anonymous and holds the web/Haku bootstrap closures, so it works
   before any credential exists (<../../../cluster/docs/nix_cache.md> § Public bootstrap
   cache). `main` is private: `web_env.sh` upserts its per-principal reader JWT
   (rotated by `attic-jwt-rotation`, `cluster/cdk8s/nix_cache/attic.py`) into
   `/nix/var/determinate/netrc` at daemon startup.
3. `github-no-proxy` git remote + `buildbuddy.remote-bazel-remote-name` for `bbr`.
4. Skills symlinked into `~/.claude/skills/` (Anthropic defaults preserved).
5. `~/.bazelrc` with a shared `--disk_cache` (50 GiB cap) at `~/.cache/bazel/disk`
   for all worktrees (web only; CLI machines:
   <../../docs/bazel_worktree_cache_sharing.md>). Shims don't pass `--nohome_rc`, so it
   applies.

**Usable disk is ~14 GiB, not ~235 GiB**: ~85% of root ext4 blocks are reserved and
unreclaimable from a session. Free space by deleting stale `.claude/worktrees/`:
<../web_env/docs/container_spec.md> § Reserved blocks.

**Install mode** (`DUCKTAPE_WEB_SETUP_MODE` or `--mode=`):

| Mode                          | Devtools + skills                                                                                                                                                                                                                                                                |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile` (default)           | `nix profile install .#devtools` (`devToolPackages` + `localOnlyPackages`); skills linked into `~/.claude/skills/`; PATH via `/usr/local/bin`.                                                                                                                                   |
| `home-manager` (experimental) | `home-manager switch --impure --flake .#claude-web` (<../../../nix/home/hosts/claude-web.nix>): `devToolPackages` only, direnv + nix-direnv, skills via <../../../nix/home/skills.nix>; PATH via `~/.nix-profile/bin`. Deploys no Claude Code settings, plugins, or MCP servers. |

The two hooks profiles (<../claude_hook/profiles/web/profile.yaml>,
<../claude_hook/profiles/web/home-manager.yaml>) are standalone copies kept in sync,
differing only in that PATH bridge.

**Flake output** (`profile` mode): `.#devtools`, or `DUCKTAPE_WEB_SETUP_OUTPUT`. Haku's
setup script (<../../../haku/runtime/claude_web_env/README.md>) sets `agent-haku`, which
adds fastmcp (`fastmcp call <url> --auth <bearer>`), himalaya, and tea; it is also
the default when the hooks profile path contains `haku`. Haku's profile materializes
tea config from `haku-forgejo-tea`; other sessions can fetch `claude-forgejo-tea`
from `claude-sandbox`.

**Secrets**: `web_setup.sh` decrypts nothing — `SOPS_AGE_KEY` reaches only the
`claude` process. The hook daemon decrypts via `startup_env_script` (`web_env.sh`)
at startup: <secrets_env_flow.md>.
