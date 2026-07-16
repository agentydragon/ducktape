#!/usr/bin/env bash
# Build all Nix flake outputs and push closures to Attic binary cache.
#
# `attic watch-store` runs in the background and uploads paths as they
# land in the local nix store, so partial progress is preserved even if
# a later build fails or the runner dies. The explicit `attic push` at
# the end is a safety net: attic dedupes by NAR hash, so paths the
# watcher already uploaded are a cheap no-op, and anything the watcher
# missed (e.g. paths from the final build that didn't drain in time)
# gets caught.
#
# TODO: drop the safety-net push once we know `attic watch-store` drains
# cleanly on SIGTERM. Today the CLI has no `--drain`/`--once` mode, so
# we don't trust the watcher alone.
set -euo pipefail

attic watch-store ducktape:main &
watcher_pid=$!
trap 'kill -TERM "$watcher_pid" 2>/dev/null || true; wait "$watcher_pid" 2>/dev/null || true' EXIT

# Single flake evaluation: build one linkFarm aggregating every target (all
# NixOS toplevels + home activationPackages + bootstrap packages). Nix
# realises/substitutes them in parallel, and watch-store uploads each as it
# lands, so partial progress survives a mid-run failure. drivefs isolation for
# the `main` cache lives in the .nix expression. Previously this was a per-host
# bash loop that cold-evaluated the flake dozens of times (attrNames + per-HM-
# user google-drive probes + a build each).
linkfarm=$(nix build --impure \
  --expr "import ./devinfra/ci/nix_attic_targets.nix { flakePath = \"path:$(pwd)\"; }" \
  --no-link --print-out-paths)

# Recover per-target store paths from the linkFarm's named symlinks — no second
# eval. bootstrap-* entries are the subset that also goes to the public cache.
out_paths=$(mktemp)
web_bootstrap_paths=$(mktemp)
for link in "$linkfarm"/*; do
  target=$(readlink "$link")
  echo "$target" >>"$out_paths"
  case "${link##*/}" in
    bootstrap-*) echo "$target" >>"$web_bootstrap_paths" ;;
  esac
done

echo "Final sweep: pushing $(wc -l <"$out_paths") paths to Attic (most already uploaded by watch-store)..."
xargs attic push ducktape:main <"$out_paths"

# The web/Haku bootstrap closures also go to the anonymous-readable `public`
# cache: a fresh Claude Code web session's first `nix profile install` runs
# before any session credential exists, so it can only substitute from a cache
# that needs no auth (see devinfra/claude/web_setup.sh, cluster/docs/nix_cache.md
# "Public bootstrap cache"). Same content as already pushed to `main` above —
# attic dedupes by NAR hash, so this is a cheap no-op for anything unchanged.
echo "Pushing $(wc -l <"$web_bootstrap_paths") web-bootstrap paths to the public Attic cache..."
xargs attic push ducktape:public <"$web_bootstrap_paths"
