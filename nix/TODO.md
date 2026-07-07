# Nix Configuration TODOs

## Consider wiring Codex OTEL export

Codex supports native OTLP exporters via `[otel]` in `config.toml`, including
logs, traces, metrics, static headers, and `log_user_prompt = true`. Consider
wiring this through `nix/home/codex/default.nix` so local Codex sessions export
to `https://alloy-otlp.allegedly.works`.

Current caveat: Codex supports static OTLP headers in config, but not a dynamic
header helper/file like Claude Code's `otelHeadersHelper`. A direct Alloy setup
would need to inject the SOPS-rendered bearer into the activation-generated
`config.toml`, so token rotation requires regenerating that config. Prefer a
dynamic helper/file upstream or a local auth-injecting forwarder if that becomes
annoying.

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

## Roll out private-cache substituter + drivefs to remaining hosts

drivefs and the `cache.allegedly.works/gaffer` substituter are wired on **wyrm2**
only (`nix/home/hosts/wyrm2.nix` `services.google-drive.enable = true`; pin in
`nix/gaffer-pins.json`). Roll the same wiring to **rugged, iguana, atlas**:

- [ ] Each host needs its own per-host SOPS attic reader file plus a parallel
      `attic-rotate-<host>-reader` CronJob (mirror the wyrm2 one in
      `cluster/k8s/agents/attic-jwt-rotation/`).
- [ ] Enable `ducktape.attic-substituter.enable = true` +
      `services.google-drive.enable = true` per host.
- [ ] Auto-fetch the gaffer pubkey post-cache-creation and PR it into
      `nix/nixos/modules/attic-substituter.nix` (TODO already noted in the module + `bootstrap.sh`).
- [ ] Split the `&ci` age recipient into `&ducktape-ci` and `&gaffer-ci` so the
      gaffer writer token isn't decryptable by ducktape CI's age key (TODO already
      noted in `.sops.yaml`).
