# Web Setup Script Debugging — 2026-03-25

Investigation into `web_setup.sh` failures in Claude Code web sessions.

## Current Status — RESOLVED

**Root cause identified and fixed.** Three independent problems combined:

1. **`max-jobs=0` in nix config** blocked all local builds, including trivial
   `symlinkJoin` derivations. Changing to `max-jobs=auto` fixes this — gVisor
   can run nix builds just fine with `sandbox=false`.

2. **Non-deterministic wheel hash** caused by `devinfra/_build_status.txt`
   (Bazel stamping file with commit hash + timestamp). Every CI run produced a
   "changed" wheel → new release → pin bump → different `symlinkJoin` hash →
   attic cache miss. Fixed by removing `devinfra.build_info` from the wheel's
   dependency tree (`52e7c39`).

3. **Anthropic caches the setup script URL at configuration time.** After
   merging `max-jobs=auto` fixes (#994, #995), sessions continued failing
   because Anthropic had fetched and cached the script when the branch-ref URL
   was first configured in the web UI — new sessions received the cached
   pre-fix script, not the updated one from GitHub. Fixed by pinning to a
   specific commit SHA (`e9f4a33`) and reconfiguring the web UI with the new
   URL — Anthropic re-fetches on URL change, so SHA-pinned URLs guarantee a
   fresh fetch. Also added `--max-jobs auto` to the `nix profile install`
   command line (`e9f4a33`) so even a stale cached script cannot re-introduce
   `max-jobs=0`.

With all three fixes applied, `nix profile install` works reliably on gVisor.
The setup script URL in the Claude Code web UI should use the pinned SHA form.

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
- `nix profile install .#devtools`

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

After we push `devtools` to attic, CI's release workflow creates a new
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

| Commit      | Attempt                                                  | Outcome                                                               |
| ----------- | -------------------------------------------------------- | --------------------------------------------------------------------- |
| `5a8a19c29` | Add `--dry-run` pre-flight cache check                   | `set -e` killed script before check ran                               |
| `64df12ede` | Put error message at tail (UI truncates to tail)         | Same — `set -e` still killed it                                       |
| `b20dd1498` | Add ix.io log upload on failure                          | Trap didn't fire (set -e killed before trap)                          |
| `f45a3adb6` | `\|\| true` on dry-run, graceful install failure         | CDN served latest, but dry-run hung (fetching nixpkgs ~50MB)          |
| `7a0457485` | Drop dry-run, add proxy env dump + connectivity check    | Got past check, confirmed proxy works                                 |
| `7db50886d` | Dump full env (redact k8s token)                         | Confirmed env vars present                                            |
| `581f2e847` | Add `cache.nixos.org` back to substituters               | Still crashed (SIGABRT from `max-jobs=0`)                             |
| `8e1eea6e7` | `max-jobs=auto` (allow local builds)                     | Still crashed — **misdiagnosed as gVisor issue** (see below)          |
| `8688fc17f` | `nix build` + manual symlinks, `max-jobs=0`              | Failed — `max-jobs=0` blocked `claude-web-session.drv`                |
| `52e7c39`   | Remove `build_info` stamping from wheel                  | Fixes spurious pin bumps (wheel hash now stable)                      |
| `842b26c`   | `max-jobs=auto`, revert to `nix profile install`         | Merged to devel; CDN still served old script for many sessions        |
| `e9f4a33`   | Add `--max-jobs auto` CLI flag, nix.conf debug dump      | Pin setup URL to this SHA → bypasses CDN → setup succeeds ✓           |
| `680d789`   | Symlink all `~/.nix-profile/bin/*` into `/usr/local/bin` | Fixes `claude-hook: not found`; also makes `bb`, `gh`, etc. available |

**Re `8e1eea6e7`**: This commit used `nix profile install` with `max-jobs=auto`.
It still failed and was diagnosed as "gVisor can't run build operations". Later
testing (same session, 2026-03-25) proved this diagnosis wrong — `nix profile
install` works fine on gVisor with `max-jobs=auto` and `sandbox=false`. The
`8e1eea6e7` failure was likely a different issue (Nix crash, network, or the
fact that `sandbox=false` wasn't set at that point).

## Fix Summary

All fixes are in place as of `680d789`:

- `max-jobs=auto` in nix.conf allows nix to build `symlinkJoin` / `buildEnv` locally
- `--max-jobs auto` on the `nix profile install` command line overrides any stale nix.conf
- Stable wheel hash (build_info removed) eliminates spurious pin bumps and cache invalidation
- Setup URL pinned to SHA bypasses Anthropic-side caching of setup script URL
- All `~/.nix-profile/bin/*` symlinked into `/usr/local/bin` — fixes `claude-hook: not found`
  and makes all Nix-installed tools (`bb`, `gh`, etc.) available to hooks and BashTool

**Setup URL** (use this in the Claude Code web UI):

```
curl -fsSL https://raw.githubusercontent.com/agentydragon/ducktape/680d78946bf72e5e3601cdb69299546b495cab1b/devinfra/claude/web_setup.sh | bash
```

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

9. **Anthropic caches the setup script at configuration time, not per-session.**
   After merging fixes to `devel`, sessions continued to receive the stale script.
   The cache is on Anthropic's side — they fetch the URL when it's saved in the
   web UI and serve that cached copy to new sessions. Changing the URL (e.g. to a
   new SHA) triggers a fresh fetch. For setup scripts that must be fresh: pin to a
   specific commit SHA and update the URL in the web UI when the script changes.
   Belt-and-suspenders: also pass critical flags (like `--max-jobs`) on the
   command line so a stale cached script can't reintroduce broken defaults.

## Container Environment (from RE docs)

- Ubuntu 24.04 on gVisor (runsc) or Firecracker microVM (current production)
- Egress proxy: TLS-inspecting, JWT auth in `HTTPS_PROXY` URL
- Proxy CA at `/usr/local/share/ca-certificates/swp-ca-production.crt`
- `environment-manager` and `process_api` binaries have been reverse-engineered —
  see <../web_env/re/environment_manager/README.md> and <../web_env/re/process_api/README.md>
- **Setup scripts run before Claude Code, as root, on EVERY session** — not just
  new sessions. `environment-manager`'s `Initialize` calls `runInitScript`
  unconditionally (Step 3), regardless of session mode (`new` / `resume` /
  `resume-cached` / `setup-only`). Steps 1 (install languages) and 2 (clone
  sources) _are_ gated on `isNewOrSetup`, which is probably where the "new
  sessions only" misconception came from. Verified in
  <../web_env/re/environment_manager/src/internal/envtype/anthropic/anthropic.go>
  Initialize(), line ~361.
- See <../web_env/re/SETUP_FLAGS_INVENTORY.md> for full binary RE docs

## Pin drift on persistent rootfs — 2026-04-14

**Symptom**: agent session's SessionStart crashes during Mako template render
with `'Undefined' object has no attribute 'kubeconfig_path'`. Alternatively:
profile YAML fields silently no-op (e.g., `startup_env_script` ignored, secrets
never decrypted). Container has been up for 2+ days; the install commit of
`claude-hooks` is behind the current `npins/sources.json` pin.

**Root cause**: `nix profile install "${FLAKE}#devtools"` is a no-op when the
attrpath is already in the profile — it matches by attrpath, not by evaluated
store hash. Firecracker microVMs persist the rootfs across sessions, and
`environment-manager` re-runs `init_script` on every session, so `web_setup.sh`
is called over and over — but each call hits the already-installed `devtools`
and skips. The nix store path for `claude-hooks` freezes at whatever pin was
current on container first-boot.

Meanwhile the working tree is `git pull`'d fresh on every session (by
environment-manager's source-handler in Step 2 for new sessions, or by our own
git usage during the session), so `profile.yaml`, `context.mako`, and other
repo files advance past what the installed `claude-hooks` wheel knows how to
parse / render. The next schema-breaking change causes a silent field drop
(Pydantic `extra="ignore"` default) or a loud template crash.

**Fix**: `web_setup.sh` now runs `nix profile remove devtools || true` before
`nix profile install`, forcing re-evaluation of `.#devtools` against the
current flake on every session. The remove+install pair is idempotent and
cheap (~1-2s steady state) because nix substitutes all closure paths from
cache when the pin hasn't actually moved.

**How to diagnose future occurrences**:

```bash
# Compare installed vs pinned claude-hooks commit
readlink /nix/var/nix/profiles/default/bin/claude-hook  # → /nix/store/<hash>-claude-hooks-<ver>/bin/claude-hook
# Then match <hash> against `git log --all --oneline` of claude-hooks source commits,
# or check `devinfra/_build_status.txt` bundled inside the wheel if present.

# Check the daemon error log for Mako/pydantic complaints
tail -100 ~/.claude/session-env/*/hook-daemon/daemon.err.log

# Check what npins says is current
python3 -c "import json; p=json.load(open('npins/sources.json'))['pins']['claude-hooks']; print(p['url'])"
```

**Lessons**:

- `nix profile install` is "add if missing" not "install-or-upgrade". Always
  pair with `nix profile remove` (or `nix profile upgrade`) on persistent
  rootfs where the install script re-runs.
- When schema-level changes land in a `claude-hooks` profile YAML or Mako
  template, they will silently break agent sessions until the next container
  rebuild unless the install script actually pulls forward the wheel.
- Pydantic `extra="ignore"` (default) silently drops unknown fields. Consider
  `extra="forbid"` on config models to turn silent drops into loud crashes.
