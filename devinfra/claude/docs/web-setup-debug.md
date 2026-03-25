# Web Setup Script Debugging — 2026-03-25

Investigation into `web_setup.sh` failures in Claude Code web sessions.

## Current Status — RESOLVED

**Root cause identified and fixed.** Two independent problems combined:

1. **`max-jobs=0` in nix config** blocked all local builds, including trivial
   `symlinkJoin` derivations. Changing to `max-jobs=auto` fixes this — gVisor
   can run nix builds just fine with `sandbox=false`.

2. **Non-deterministic wheel hash** caused by `devinfra/_build_status.txt`
   (Bazel stamping file with commit hash + timestamp). Every CI run produced a
   "changed" wheel → new release → pin bump → different `symlinkJoin` hash →
   attic cache miss. Fixed by removing `devinfra.build_info` from the wheel's
   dependency tree (`52e7c39`).

With both fixes, `nix profile install` works reliably on gVisor.

### Correction: gVisor CAN run nix builds

Earlier iterations (`8e1eea6e7`) concluded "gVisor can't run build operations".
This was wrong. The `8e1eea6e7` test used `nix profile install` which tries
to build a `buildEnv` wrapper depending on `patchelf` → `xz` → bootstrap chain.
Those builds may have failed for other reasons (Nix sandbox remnants, cascading
failures causing SIGABRT). When tested in isolation with `sandbox=false` and
`max-jobs=auto`, all of the following work on gVisor:

- Trivial derivations (`/bin/sh -c "echo hello > $out"`)
- `pkgs.runCommand`
- `pkgs.symlinkJoin`
- `nix profile install .#web-session`

No gVisor syscall issues were observed for any nix build operation.

## Root Causes (as originally diagnosed — corrections inline)

### 1. ~~`nix profile install` needs build-time tools unavailable on gVisor~~

**Incorrect.** `nix profile install` does need to build a `buildEnv` wrapper,
but this builds fine on gVisor with `sandbox=false` and `max-jobs=auto`.
The original failure was likely caused by `max-jobs=0` (which blocked the
build entirely) combined with Nix 2.34.3's SIGABRT crash on build failures,
which obscured the real error.

### 2. `max-jobs=0` blocks even trivial local builds — CONFIRMED

With `max-jobs=0`, Nix can't build anything locally — not even a trivial
`symlinkJoin` that just creates symlinks. This was the primary blocker.

### 3. Attic cache race with CI pin bumps — CONFIRMED

After we push `web-session` to attic, CI's release workflow creates a new
commit bumping `npins/sources.json` artifact pins. The web container evaluates
`github:agentydragon/ducktape` (latest commit), which is now the CI commit
with different pins → different `claude-hooks` derivation → different
`symlinkJoin` hash → cache miss.

**Root cause of the spurious pin bumps**: the `claude-hooks` wheel bundled
`devinfra/_build_status.txt` via the `//devinfra:build_info` dep chain. This
file embeds `STABLE_BUILD_COMMIT` and `STABLE_BUILD_TIMESTAMP` from Bazel's
`ctx.info_file`, which change on every build. The two wheels between pins
`8e1eea6` and `8688fc1` had identical Python code — the only difference was
the build stamp. CI's `maybe_release` compared raw wheel hashes and saw a
"change". Fixed in `52e7c39` by removing `build_info` entirely.

### 4. Nix 2.34.3 crash on cascading build failures — CONFIRMED

When builds fail with `max-jobs=0`, Nix hits an assertion failure in
`Goal::amDone` (exit 134 / SIGABRT) instead of reporting a clean error.
This is a known Nix bug, and it masked the real `max-jobs=0` root cause
during early debugging.

## Symptoms

### `nix profile install` with `max-jobs=0` (original)

- Exit code 134 (SIGABRT) from `nix profile install`
- Stack trace: `Assertion 'result == ecSuccess || result == ecFailed || result == ecNoSubstituters' failed`
- Misleadingly reported as build failures for `patchelf-0.15.2.drv`, `xz-5.8.1.drv`

### `nix build` + symlinks with `max-jobs=0` (8688fc17f)

- Exit code 1 from `nix build`
- Clean error: `Cannot build ... Reason: local builds are disabled (max-jobs = 0)`
- All 158 dependency substitutions succeed; only the top-level `claude-web-session.drv` fails

### `nix profile install` with `max-jobs=auto` — WORKS

- All dependencies fetched from caches
- `symlinkJoin` and `buildEnv` build locally without error
- Full `web-session` profile installed successfully

## Environment Findings

- **No DNS** in gVisor containers — all connections go through CONNECT proxy
- `HTTPS_PROXY` / `HTTP_PROXY` are set by the container with JWT credentials
- `curl` works (uses proxy env vars)
- Nix uses libcurl internally and respects proxy env vars — verified by
  Nix successfully fetching the flake from `github:agentydragon/ducktape`
  and querying `cache.allegedly.works`
- The egress proxy allows all destinations (user-configured)
- `NIX_SSL_CERT_FILE` is set by `nix.sh` to `/etc/ssl/certs/ca-certificates.crt`,
  which includes the proxy CA (pre-installed at
  `/usr/local/share/ca-certificates/swp-ca-production.crt`)

## Timeline of Debugging Iterations

| Commit      | Attempt                                               | Outcome                                                      |
| ----------- | ----------------------------------------------------- | ------------------------------------------------------------ |
| `5a8a19c29` | Add `--dry-run` pre-flight cache check                | `set -e` killed script before check ran                      |
| `64df12ede` | Put error message at tail (UI truncates to tail)      | Same — `set -e` still killed it                              |
| `b20dd1498` | Add ix.io log upload on failure                       | Trap didn't fire (set -e killed before trap)                 |
| `f45a3adb6` | `\|\| true` on dry-run, graceful install failure      | CDN served latest, but dry-run hung (fetching nixpkgs ~50MB) |
| `7a0457485` | Drop dry-run, add proxy env dump + connectivity check | Got past check, confirmed proxy works                        |
| `7db50886d` | Dump full env (redact k8s token)                      | Confirmed env vars present                                   |
| `581f2e847` | Add `cache.nixos.org` back to substituters            | Still crashed (SIGABRT from `max-jobs=0`)                    |
| `8e1eea6e7` | `max-jobs=auto` (allow local builds)                  | Still crashed — **misdiagnosed as gVisor issue** (see below) |
| `8688fc17f` | `nix build` + manual symlinks, `max-jobs=0`           | Failed — `max-jobs=0` blocked `claude-web-session.drv`       |
| `52e7c39`   | Remove `build_info` stamping from wheel               | Fixes spurious pin bumps (wheel hash now stable)             |

**Re `8e1eea6e7`**: This commit used `nix profile install` with `max-jobs=auto`.
It still failed and was diagnosed as "gVisor can't run build operations". Later
testing (same session, 2026-03-25) proved this diagnosis wrong — `nix profile
install` works fine on gVisor with `max-jobs=auto` and `sandbox=false`. The
`8e1eea6e7` failure was likely a different issue (Nix crash, network, or the
fact that `sandbox=false` wasn't set at that point).

## Remaining Fix

Change `max-jobs = 0` to `max-jobs = auto` in `web_setup.sh`. Combined with
the build_info removal (`52e7c39`), this should make web setup work reliably:

- `max-jobs=auto` allows nix to build `symlinkJoin` / `buildEnv` locally
- Stable wheel hash eliminates spurious pin bumps and cache invalidation
- Even if a cache miss occurs, the local build succeeds

## Key Lessons

1. **gVisor CAN run nix builds** with `sandbox=false` and `max-jobs=auto`.
   Earlier conclusions to the contrary were wrong — the SIGABRT crash from
   `max-jobs=0` masked the real error.

2. **`max-jobs=0` + Nix 2.34.3 = misleading SIGABRT**: Nix crashes with an
   assertion failure instead of a clean "local builds disabled" message. This
   sent debugging down the wrong path (investigating gVisor syscall support
   instead of just allowing local builds).

3. **Non-deterministic build stamps poison cache chains**: a stamping file that
   changes every build makes the wheel hash non-deterministic, which triggers
   spurious releases and pin bumps downstream.

4. **`attic push` only pushes the closure of what you give it**: the paths
   in our web-session closure don't include Nix's profile machinery.

5. **UI truncates setup script output to the tail** — put actionable info last.

6. **No DNS in gVisor containers** — all network must go through the HTTPS
   CONNECT proxy. curl and Nix (via libcurl) both handle this via env vars.

7. **`set -euo pipefail` + `$(failing_command)` = silent death**: the script
   exits before any error handling runs.

8. **CI pin-bump race**: pushing to attic from a local machine doesn't help
   if CI pushes a pin-bump commit before the web session starts, changing
   the derivation hash. Fixed by making wheel hash deterministic.

## Container Environment (from RE docs)

- Ubuntu 24.04 on gVisor (runsc)
- Egress proxy: TLS-inspecting, JWT auth in `HTTPS_PROXY` URL
- Proxy CA at `/usr/local/share/ca-certificates/swp-ca-production.crt`
- No open-sourced environment-manager or process_api binaries
- Setup scripts run before Claude Code, as root, on new sessions only
- See <../web_env/re/SETUP_FLAGS_INVENTORY.md> for full binary RE docs
