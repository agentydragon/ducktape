# Nix Installation Timing Report

**Environment**: Claude Code Web (gVisor sandbox)
**Date**: 2026-01-08
**Nix Version**: 2.33.0

## Summary

This report documents installation times and storage requirements for nix and development tools in the Claude Code Web environment. All measurements use the official **cache.nixos.org** binary cache.

## Base Nix Installation

The nix installer downloads a pre-built binary tarball and unpacks it.

| Component              | Download | Unpacked | Time |
| ---------------------- | -------- | -------- | ---- |
| Nix binary tarball     | 23.4 MB  | ~98 MB   | ~10s |
| Unpacking & setup      | -        | -        | ~5s  |
| **Total base install** | 23.4 MB  | ~98 MB   | ~15s |

## Tool Installation via Nix (with Binary Cache)

All tools installed using `nix profile install nixpkgs#<tool>`. Times include downloading pre-built binaries from cache.nixos.org.

### First Tool Installation (Cold Start)

The first `nix profile install` must fetch and unpack the nixpkgs git repository (~50MB) before evaluating derivations. This is a one-time cost.

| Tool      | Download | Unpacked | Time     | Notes                           |
| --------- | -------- | -------- | -------- | ------------------------------- |
| alejandra | 9.6 MB   | 43.2 MB  | **117s** | Includes nixpkgs metadata fetch |

### Second Tool Installation (Warm-up)

Second install is faster but still has some overhead as nix caches more metadata.

| Tool     | Download | Unpacked | Time    |
| -------- | -------- | -------- | ------- |
| bazelisk | 2.4 MB   | 9.1 MB   | **21s** |

### Subsequent Tool Installations (Steady State)

After the first two installs, nixpkgs is fully cached and installs are fast.

| Tool     | Download | Unpacked | Time     |
| -------- | -------- | -------- | -------- |
| opentofu | 21.3 MB  | 108.2 MB | **5.5s** |
| tflint   | 10.9 MB  | 49.6 MB  | **3.4s** |

## Comparison: Nix vs Direct Binary Download

The session hook uses direct binary downloads for some tools. Here's a comparison:

| Tool      | Binary Download | Nix (steady state) | Winner     |
| --------- | --------------- | ------------------ | ---------- |
| opentofu  | ~2s, 28 MB      | ~6s, 108 MB        | Binary     |
| tflint    | ~2s, 24 MB      | ~3s, 50 MB         | Binary     |
| flux      | ~2s, 21 MB      | ~6s, 110 MB        | Binary     |
| kubeseal  | ~1s, 48 MB      | N/A                | Binary     |
| kustomize | ~1s, 14 MB      | N/A                | Binary     |
| helm      | ~2s, 55 MB      | N/A                | Binary     |
| alejandra | N/A             | ~3s, 9 MB          | Nix (only) |

**Tradeoffs**:

- **Binary downloads**: Faster, smaller, but requires maintaining version URLs
- **Nix**: Slower, larger, but declarative and reproducible

## Cold Start Cost Summary

For a fresh nix installation installing just alejandra:

| Phase                  | Time      |
| ---------------------- | --------- |
| Nix base install       | 15s       |
| First tool (+ nixpkgs) | 117s      |
| **Total cold start**   | **~130s** |

For subsequent tools after cold start: **3-6s each**

## Storage Summary

| Configuration              | Total Storage |
| -------------------------- | ------------- |
| Nix base only              | ~98 MB        |
| Nix + alejandra + deps     | ~315 MB       |
| Nix + 4 tools              | ~480 MB       |
| Binary downloads (6 tools) | ~190 MB       |

## Conclusion: Nix Not Viable for Claude Code Web

**130s cold start exceeds the ~60s session hook timeout.** The hook would be killed before nix finishes installing even one tool.

### Why Nix Doesn't Work Here

1. **Hook timeout**: Session start hooks have a ~40-60s timeout; nix needs 130s+
2. **No persistent storage**: Each session starts fresh, so the 117s nixpkgs fetch happens every time
3. **Dominated by metadata**: The actual tool download (9.6 MB) takes ~3s; the nixpkgs eval takes 110s+

### Recommended Strategy for Claude Code Web

| Tool Type      | Strategy                     | Time |
| -------------- | ---------------------------- | ---- |
| Go/Rust CLIs   | Direct binary download       | 1-2s |
| Node tools     | npm/npx                      | 2-5s |
| Python tools   | pip/pipx                     | 2-5s |
| Nix-only tools | **Skip or find alternative** | -    |

For alejandra specifically: either skip nix formatting in Claude Web sessions, or find/build a standalone binary.
