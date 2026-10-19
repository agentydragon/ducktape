@README.md

## Rules

- **Statusline deps live in two lists**: `requires` of
  `//devinfra/claude/statusline:claude_statusline_wheel` and `claude-statusline`'s
  `propagatedBuildInputs` in `nix/packages/default.nix` (both marked `SYNC:`). Update
  both, or `claude-statusline` fails with `ModuleNotFoundError` where the list is
  stale.
- **`bb remote` behavior** (remote selection, flag semantics, git config keys): read
  the source — <https://github.com/buildbuddy-io/buildbuddy>
  `cli/remotebazel/remotebazel.go`, `cli/storage/storage.go`.
- **Platform**: web and CLI-managed remote sessions run on Firecracker
  (`--firecracker-init` on PID 1's cmdline); the hook uses that only to size Bazel's
  JVM heap. Specs and IO benchmarks: <web_env/docs/container_spec.md>. If the marker
  is absent in a live web/remote session, stop and report platform drift; don't
  apply old container-runtime workarounds.

## Debugging

SessionStart crash → `daemon.err.log` first; `startup_failure.json` means the
dispatcher never reached the daemon.

```bash
LIVE=$(basename "$(dirname "$CLAUDE_ENV_FILE")")
tail -100 "/tmp/claude-hd/$LIVE/daemon.err.log" "/tmp/claude-hd/$LIVE/daemon.log"
cat "$HOME/.claude/session-env/$LIVE/bazelrc"
claude-hook --version   # crate version, not the release pin
jq -r '.pins["claude-hook"].url, .pins["claude-statusline"].url' nix/artifact-pins.json
```

Stale install → re-run `bash devinfra/claude/web_setup.sh` (refreshes `.#devtools`,
which carries both binaries); cause: <docs/web-setup-debug.md> § Pin Drift On
Persistent Rootfs.

## Container lifecycle (reverse-engineered)

Before speculating about when session-start-adjacent things fire, consult the
partial RE of Anthropic's garble-obfuscated Go `environment-manager` under
<web_env/re/environment_manager/> — grep `src/` for the function name or error string
(decompiled files carry line-annotated binary offsets). It is incomplete and may be
wrong: inside a web session, pull the live binary and verify (and update the RE)
before treating it as authoritative.

- **Mode gating** (`new`/`resume`/`resume-cached`/`setup-only`),
  `Initialize()` in
  <web_env/re/environment_manager/src/internal/envtype/anthropic/anthropic.go>:
  steps 1–3 (install languages, clone sources, init script = `web_setup.sh`) run
  only for `new`/`setup-only` — **`resume` and `resume-cached` skip the init
  script**. Steps 4–6 (`claude --init-only` firing SessionStart, skill bootstrap,
  hook bootstrap) run for `new` and `resume`; **`resume-cached` exits early**;
  `setup-only` exits partway through them.
- **Unverified on Build ID `0b86a2a0`**: there the envtype is garbled package
  `CWddODOS8sH` (<web_env/re/environment_manager/degarble_map.md>) and `Initialize`
  grew 103 → 157 symbols, cause unestablished. Established: it wraps seven phases in
  timing closures (`startup_timing` / `phase_start_ms` telemetry) — not evidence of a
  seventh lifecycle step.
- Addresses are per-build (garble re-randomizes symbols); re-derive with `gosymtab`
  (<web_env/re/environment_manager/README.md>), never carry them forward.
- `process_api` (PID 1) lifecycle, WebSocket ports, orphan monitor, OOM killers:
  <web_env/re/process_api/README.md>. Tuning flags/env vars:
  <web_env/re/SETUP_FLAGS_INVENTORY.md>.
