#!/usr/bin/env bash
# Removes top-level /tmp dirs that tools leak per run, once untouched for a day.
set -euo pipefail

find /tmp -mindepth 1 -maxdepth 1 -type d -mmin +1440 \( \
  -name 'claude-empty-*' -o -name 'cdk8s.outdir.*' -o -name 'scoped_dir*' \
  \) -exec rm -rf --one-file-system {} +

# Empty only: a nix shell open for over a day keeps a stale-mtime dir in use.
find /tmp -mindepth 1 -maxdepth 1 -type d -mmin +1440 -empty \( \
  -name 'nix-shell.*' -o -name 'nix-develop-*' \
  \) -delete
