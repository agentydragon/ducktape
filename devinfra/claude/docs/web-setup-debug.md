# Web Setup Script Debugging — 2026-03-25

Investigation into `web_setup.sh` failures in Claude Code web sessions.

## Current Status

**`nix profile install` cannot work on gVisor.** It requires building a
`buildEnv` wrapper derivation that depends on `patchelf` and other build-time
tools. These tools exist in `cache.nixos.org` but their build-time
_dependencies_ fail to build on gVisor (missing syscalls / proc limitations),
and `max-jobs=0` blocks local builds entirely.

**Latest attempt** (`8688fc17f`): replaced `nix profile install` with
`nix build` + manual symlinks into `~/.nix-profile/{bin,share}/`. This avoids
the profile machinery. **Verified: still fails.**

**Observed 2026-03-25**: All 158 store path substitutions succeeded (2.8 MiB
download, 612.8 MiB unpacked from `cache.allegedly.works` and `cache.nixos.org`).
The final `claude-web-session.drv` was not in any cache and required a local
build, which `max-jobs=0` blocked:

```
error: Cannot build '/nix/store/7vbnxlhl65wrcz9r2mwx1n173iwrg00g-claude-web-session.drv'.
       Reason: local builds are disabled (max-jobs = 0)
       Hint: set 'max-jobs' to a non-zero value to enable local builds, or configure remote builders via 'builders'
```

This confirms the predicted risk: the `symlinkJoin` derivation hash changed
between the attic push and the web session evaluation (CI pin-bump race),
causing a cache miss for the top-level derivation.

**Root cause of the pin bump** (investigated same session): commit `5ed94a2`
(`chore: bump release artifacts`) changed `npins/sources.json` because CI
detected a different wheel hash for `claude-hooks`. However, the only file
changed in the wheel's source commit (`8688fc17f`) was `web_setup.sh`, which
is **not in the wheel** — the wheel contains only Python modules from
`py_package`. The wheel hash changed because `devinfra/_build_status.txt`
(a Bazel stamping file embedding commit hash + timestamp via `ctx.info_file`)
differs on every build. This meant *every* CI run produced a "changed" wheel,
triggered a release, and bumped the pin — even with zero code changes.

**Fix**: removed `devinfra.build_info` (the stamping module) from the wheel's
dependency tree entirely. The session context template no longer includes a
build commit hash.

## Root Causes (multiple)

### 1. `nix profile install` needs build-time tools unavailable on gVisor

`nix profile install` creates a `buildEnv` wrapper → needs `patchelf` →
needs `xz` → needs bootstrap chain. Even with `sandbox=false` and
`max-jobs=auto`, these builds **fail on gVisor** (tested in `8e1eea6e7`).
The gVisor kernel doesn't support all syscalls needed by the Nix build
sandbox.

### 2. `max-jobs=0` blocks even trivial local builds

With `max-jobs=0`, Nix can't build anything locally — not even a trivial
`symlinkJoin` that just creates symlinks. Everything must come from a
substituter (binary cache).

### 3. Attic cache race with CI pin bumps

After we push `web-session` to attic, CI's release workflow creates a new
commit bumping `npins/sources.json` artifact pins. The web container evaluates
`github:agentydragon/ducktape` (latest commit), which is now the CI commit
with different pins → different `claude-hooks` derivation → different
`symlinkJoin` hash → cache miss.

### 4. Nix 2.34.3 crash on cascading build failures

When builds fail with `max-jobs=0`, Nix hits an assertion failure in
`Goal::amDone` (exit 134 / SIGABRT) instead of reporting a clean error.
This is a known Nix bug.

## Symptoms

### `nix profile install` (original approach)

- Exit code 134 (SIGABRT) from `nix profile install`
- Stack trace: `Assertion 'result == ecSuccess || result == ecFailed || result == ecNoSubstituters' failed`
- Build failures for `patchelf-0.15.2.drv`, `xz-5.8.1.drv` (bootstrap chain)

### `nix build` + symlinks (8688fc17f)

- Exit code 1 from `nix build`
- Clean error: `Cannot build ... Reason: local builds are disabled (max-jobs = 0)`
- All 158 dependency substitutions succeed; only the top-level `claude-web-session.drv` fails

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
- patchelf and xz store paths ARE in `cache.nixos.org` (verified with
  `curl` narinfo lookups returning 200), but their dependencies fail to build

## Timeline of Debugging Iterations

| Commit      | Attempt                                               | Outcome                                                      |
| ----------- | ----------------------------------------------------- | ------------------------------------------------------------ |
| `5a8a19c29` | Add `--dry-run` pre-flight cache check                | `set -e` killed script before check ran                      |
| `64df12ede` | Put error message at tail (UI truncates to tail)      | Same — `set -e` still killed it                              |
| `b20dd1498` | Add ix.io log upload on failure                       | Trap didn't fire (set -e killed before trap)                 |
| `f45a3adb6` | `\|\| true` on dry-run, graceful install failure      | CDN served latest, but dry-run hung (fetching nixpkgs ~50MB) |
| `7a0457485` | Drop dry-run, add proxy env dump + connectivity check | Got past check, confirmed proxy works                        |
| `7db50886d` | Dump full env (redact k8s token)                      | Confirmed env vars present                                   |
| `581f2e847` | Add `cache.nixos.org` back to substituters            | Still crashed — builds fail on gVisor even with caches       |
| `8e1eea6e7` | `max-jobs=auto` (allow local builds)                  | Still crashed — gVisor can't run build operations            |
| `8688fc17f` | `nix build` + manual symlinks (no profile install)    | **Failed** — `max-jobs=0` blocked `claude-web-session.drv` (cache miss) |

## Next Actions

`nix build` + symlinks confirmed broken (2026-03-25). Two options, in order
of preference:

**Option A: Pre-computed store path** — CI records the `web-session` store
path after `attic push`. The setup script fetches it with `nix copy --from`
(no evaluation, no building). See <nix-speed-options.md> Option 1.

```bash
# CI writes store path to a known URL or file in the repo
STORE_PATH=$(curl -fsSL "$FLAKE_RAW/devinfra/claude/web-session-store-path.txt")
nix copy --from https://cache.allegedly.works/main "$STORE_PATH"
ln -sfn "$STORE_PATH"/bin/* ~/.nix-profile/bin/
```

This completely eliminates evaluation (no nixpkgs fetch, no flake eval) and
building (pure substitution by known path). Immune to the CI pin-bump race.

**Option B: Nix store tarball** — CI exports the closure as a tarball,
setup script downloads and unpacks to `/nix/store`. No Nix needed at runtime
beyond the initial install. See <nix-speed-options.md> Option 2.

## Key Lessons

1. **`nix profile install` ≠ `nix build`**: profile install creates a
   `buildEnv` wrapper that needs build-time tools not in the runtime closure.

2. **gVisor can't run arbitrary Nix builds**: even with `sandbox=false`, the
   gVisor kernel lacks syscalls needed by build operations. `max-jobs=auto`
   doesn't help — builds themselves fail, not just the permission to build.

3. **`attic push` only pushes the closure of what you give it**: the 143 paths
   in our web-session closure don't include Nix's profile machinery.

4. **Nix 2.34.3 crashes on cascading build failures** instead of reporting a
   clean error. The assertion in `Goal::amDone` is a known bug.

5. **UI truncates setup script output to the tail** — put actionable info last.

6. **No DNS in gVisor containers** — all network must go through the HTTPS
   CONNECT proxy. curl and Nix (via libcurl) both handle this via env vars.

7. **`set -euo pipefail` + `$(failing_command)` = silent death**: the script
   exits before any error handling runs.

8. **CI pin-bump race**: pushing to attic from a local machine doesn't help
   if CI pushes a pin-bump commit before the web session starts, changing
   the derivation hash.

## Container Environment (from RE docs)

- Ubuntu 24.04 on gVisor (runsc)
- Egress proxy: TLS-inspecting, JWT auth in `HTTPS_PROXY` URL
- Proxy CA at `/usr/local/share/ca-certificates/swp-ca-production.crt`
- No open-sourced environment-manager or process_api binaries
- Setup scripts run before Claude Code, as root, on new sessions only
- See <../web_env/re/SETUP_FLAGS_INVENTORY.md> for full binary RE docs
