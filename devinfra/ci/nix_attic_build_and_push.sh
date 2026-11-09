#!/usr/bin/env bash
# Build one destination's Nix flake output and push its closure to Attic.
#
# The workflow runs `main` and `public` in separate matrix jobs: a broken host
# configuration must not prevent the independent public bootstrap closure from
# reaching the cache. nix-fast-build evaluates the selected output in parallel
# (nix-eval-jobs), builds/substitutes each derivation, and uploads it to Attic as
# it finishes — so partial progress survives a mid-run failure. --skip-cached
# queries the substituters and skips paths already in the cache, so a commit that
# changes nothing in a closure neither rebuilds nor re-downloads it.
#
# --option pure-eval false: the flake needs impure eval (NixOS hosts read
# /etc/nixos via builtins.pathExists). nix-fast-build has no --impure flag, but
# pure-eval=false covers path access. The one impurity it does NOT expose is
# nixGL's builtins.currentTime driver-sniffing — forced off in these target
# outputs (see devinfra/ci/nix_attic_targets.nix), which also bakes in the drivefs
# isolation (google-drive off for wyrm2/rugged) so the private gaffer closure
# never enters `main`. See cluster/docs/nix_cache.md "Private-binary isolation".
set -euo pipefail

flake=".#atticPushTargets.x86_64-linux"

case "${1:-}" in
  main)
    # Every target → the broadly readable `main` cache; skip paths already present.
    nix-fast-build --no-nom --option pure-eval false --skip-cached \
      --flake "${flake}.main" --attic-cache ducktape:main
    ;;
  public)
    # The bootstrap subset → the anonymous `public` cache. A fresh Claude Code
    # web session has no credential yet, so it can substitute only this cache
    # (see devinfra/claude/web_setup.sh, cluster/docs/nix_cache.md "Public
    # bootstrap cache"). No --skip-cached: its query cannot see `public`, so it
    # would wrongly skip paths in `main` but absent from `public`.
    nix-fast-build --no-nom --option pure-eval false \
      --flake "${flake}.public" --attic-cache ducktape:public
    ;;
  *)
    echo "usage: $0 {main|public}" >&2
    exit 2
    ;;
esac
