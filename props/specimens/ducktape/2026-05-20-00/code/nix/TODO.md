# Nix Configuration TODOs

## Nix PATH not available in non-interactive shells (mosh issue)

**Problem:** On non-NixOS systems with nix installed, mosh fails to find nix-installed `mosh-server` because:

1. mosh runs `mosh-server` via SSH in a **non-login, non-interactive shell**
2. The nix installer only adds PATH setup to:
   - `/etc/profile.d/nix.sh` (login shells via `/etc/profile`)
   - `/etc/zsh/zshrc` (interactive zsh shells)
3. Neither runs for non-interactive shells, so `which mosh-server` finds `/usr/bin/mosh-server` (old system version) instead of `~/.nix-profile/bin/mosh-server`

**Symptoms:**

- mosh true color broken (mosh 1.4.0 required, but system has 1.3.2)
- Any nix-installed binary unavailable when running commands via `ssh host 'command'`

**Current workaround:** Added nix-daemon.sh sourcing to `programs.zsh.envExtra` in home.nix (writes to `~/.zshenv` which runs for ALL zsh invocations).

**Proper fix:** The nix installer should add sourcing to `/etc/zsh/zshenv` instead of/in addition to `/etc/zsh/zshrc`. This is arguably a gap in nix's shell integration.

## Deduplicate wyrm2/rugged/iguana host configs

wyrm2, rugged, and iguana import nearly identical module sets (gui, workstation, bazel, system-inspection-sudo, k8s-worker) and have similar `k8sWorker` config. Extract common setup into a shared module (e.g., `modules/k8s-workstation.nix`).

## Rugged: evaluate `systemd-homed`

Consider whether `nix/nixos/hosts/rugged` should switch from the current single `cryptroot` layout to a `systemd-homed`-style design for tablet-friendly graphical login and per-user home unlock. Evaluate tradeoffs versus keeping full-disk LUKS and adding a non-keyboard pre-boot unlock path such as FIDO2.

## TTY password feedback

Evaluate whether console TTY password prompts can show visual feedback (for example `*` per keystroke) on NixOS hosts. This is not a standard NixOS knob like `sudo` `pwfeedback`; TTY login goes through `agetty` into `login`/PAM, so this likely requires a downstream package override or alternate login program. Scope and risks need review before implementing.

## Wire Claude Code OTEL export to cluster collector

Add OTEL env vars to `nix/home/claude_code/default.nix` `settings.env` block to export traces/logs/metrics to the cluster's OTEL collector endpoint. Key vars: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER`, `OTEL_METRICS_EXPORTER`. Consider also `OTEL_LOG_TOOL_CONTENT=1` and `OTEL_LOG_TOOL_DETAILS=1` for full tool visibility. The `SRT_DEBUG=1` env var (already configured) provides sandbox-level network logging; OTEL would give structured traces for API calls, tool execution, and query latency.
