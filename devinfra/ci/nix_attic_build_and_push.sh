#!/usr/bin/env bash
# Build every Nix flake output and push its closure to the Attic binary cache.
#
# nix-fast-build evaluates the ci-attic-* flake outputs in parallel
# (nix-eval-jobs), builds/substitutes each derivation, and uploads it to Attic as
# it finishes — so partial progress survives a mid-run failure. --skip-cached
# queries the substituters and skips paths already in the cache, so a commit that
# changes nothing in a closure neither rebuilds nor re-downloads it.
#
# --option pure-eval false: the flake needs impure eval (NixOS hosts read
# /etc/nixos via builtins.pathExists). nix-fast-build has no --impure flag, but
# pure-eval=false covers path access. The one impurity it does NOT expose is
# nixGL's builtins.currentTime driver-sniffing — forced off in the ci-attic-*
# outputs (see devinfra/ci/nix_attic_targets.nix), which also bakes in the drivefs
# isolation (google-drive off for wyrm2/rugged) so the private gaffer closure
# never enters `main`. See cluster/docs/nix_cache.md "Private-binary isolation".
set -euo pipefail

flake=".#legacyPackages.x86_64-linux"

# Every target → the broadly readable `main` cache; skip paths already present.
nix-fast-build --no-nom --option pure-eval false --skip-cached \
  --flake "${flake}.ci-attic-main" --attic-cache ducktape:main

# The bootstrap subset also goes to the anonymous-readable `public` cache: a fresh
# Claude Code web session's first `nix profile install` runs before any
# credential exists, so it can only substitute from a no-auth cache (see
# devinfra/claude/web_setup.sh, cluster/docs/nix_cache.md "Public bootstrap
# cache"). No --skip-cached here: its substituter query can't see `public`, so it
# would wrongly skip paths present in `main` but missing from `public`. attic
# dedupes by NAR hash server-side, so re-pushing costs only the presence check.
nix-fast-build --no-nom --option pure-eval false \
  --flake "${flake}.ci-attic-public" --attic-cache ducktape:public
