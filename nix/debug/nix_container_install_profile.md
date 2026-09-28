# Nix Install Profiling — Container (x86_64-linux)

**Date**: 2026-03-22
**Nix version installed**: 2.34.2
**Platform**: x86_64-linux container (running as root)
**Installer**: Official nixos.org single-user installer (`--no-daemon`)

## Results Summary

| Metric                                         | Value                              |
| ---------------------------------------------- | ---------------------------------- |
| Tarball download size                          | **24.4 MB** (compressed `.tar.xz`) |
| Download speed (observed)                      | ~32–44 MB/s                        |
| Download time                                  | ~0.5–1s at those speeds            |
| Post-install `/nix/` disk usage                | **599 MB**                         |
| Post-install `/nix/store/` disk usage          | **591 MB**                         |
| Store entries (paths)                          | **73**                             |
| Total wall-clock time (second run, successful) | **~21s**                           |

## Phases Observed

1. **Download tarball** (`nix-2.34.2-x86_64-linux.tar.xz`, 24.4 MB) — fast, ~1s at 32–44 MB/s
2. **Copy Nix to `/nix/store/`** — unpacking ~591 MB from the 24.4 MB compressed tarball
3. **Install `nix-2.34.2` profile** — builds `user-environment.drv`, unpacks 1 channel
4. **Modify shell configs** — `.profile`, `.zshrc`, fish config

## Gotchas in a Root Container

- The official installer warns: _"installing Nix as root is not supported by this script"_
- Running as root without a `nixbld` group causes failure at the profile installation step.
  **Fix**: create the group and users before running the installer:
  ```bash
  groupadd nixbld
  for i in $(seq 1 32); do
    useradd -g nixbld -G nixbld -M -N -r -s /sbin/nologin nixbld$i
  done
  ```
- The hostname resolution warning (`unable to resolve host (none)`) from `sudo mkdir /nix` is harmless if `/nix` already exists and is writable.

## Network Traffic

- ~24.4 MB downloaded (the tarball)
- ~60 MB additional RX on second run (re-downloaded; installer does not cache between runs)
- The installer always re-downloads the tarball to a fresh `/tmp` dir — no resume/cache

## Expansion Ratio

Tarball: 24.4 MB compressed → 591 MB on disk ≈ **24× expansion**

## Alternatives Considered

| Method                                    | Archive size | Restore time | Notes                                             |
| ----------------------------------------- | ------------ | ------------ | ------------------------------------------------- |
| Official installer (with nixpkgs channel) | 24.4 MB xz   | ~21s         | **chosen** — simplest, no artifact to host        |
| zstd snapshot, minimal (no nixpkgs)       | 35 MB        | 0.5s         | faster but requires CI to build+ship the snapshot |
| `nix copy` binary cache (closure only)    | ~122 MB      | ~1–2s        | no Nix needed at eval time, just import           |

The zstd snapshot and binary cache approaches were prototyped but discarded in favour of the
official installer: the installer requires no hosted artifact, and ~21s is acceptable for a
one-time web session setup. The main cost is disk I/O (unpacking), not network (~1s download).

## Nix Profile Location

```
/root/.nix-profile -> /nix/var/nix/profiles/default
```

Source into shell with:

```bash
. ~/.nix-profile/etc/profile.d/nix.sh
```
