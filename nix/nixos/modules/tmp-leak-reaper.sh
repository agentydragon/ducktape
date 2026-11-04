#!/usr/bin/env bash
# Removes entries that tools leak per run, once untouched for a day. Without arguments, scans /tmp.
# -ignore_readdir_race: live sessions create and remove these entries while find scans, and
# find otherwise exits 1 on an entry that vanished between readdir and stat.
set -euo pipefail

if (($# == 0)); then
  set -- /tmp
fi
for dir in "$@"; do
  [[ -d $dir && ! -L $dir ]] || continue

  # bun-* and uv-setuptools-* are files; the rest are dirs.
  find "$dir" -mindepth 1 -maxdepth 1 -ignore_readdir_race \( -type d -o -type f \) -mmin +1440 \( \
    -name 'claude-empty-*' -o -name 'cdk8s.outdir.*' -o -name 'scoped_dir*' \
    -o -name 'pip-*' -o -name 'pyright-*' -o -name 'bun-*' -o -name 'uv-setuptools-*' \
    \) -exec rm -rf --one-file-system {} +

  # bb run leaves its generated launcher behind after builds succeed or fail.
  find "$dir" -mindepth 1 -maxdepth 1 -ignore_readdir_race -type f -mmin +1440 -name 'bb-run-*' -delete

  # Empty only: a nix shell open for over a day keeps a stale-mtime dir in use.
  find "$dir" -mindepth 1 -maxdepth 1 -ignore_readdir_race -type d -mmin +1440 -empty \( \
    -name 'nix-shell.*' -o -name 'nix-develop-*' \
    \) -delete
done
