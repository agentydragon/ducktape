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

Check the running `environment-manager` binary in the web session before relying on startup or
session-mode behavior. See the [build-specific lifecycle evidence](web_env/re/environment_manager/README.md#session-startup-by-build).

For PID 1 lifecycle, WebSocket ports, orphan monitoring and OOM handling, see the
[process_api guide](web_env/re/process_api/README.md). For flags and environment variables, see the
[setup flag inventory](web_env/re/SETUP_FLAGS_INVENTORY.md).
