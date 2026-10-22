#!/usr/bin/env bash
set -euo pipefail
worktree_root=""
for arg in "$@"; do
  case "$arg" in
    --worktree_root=*) worktree_root="${arg#*=}" ;;
  esac
done
if [[ -z "$worktree_root" ]]; then
  echo "missing --worktree_root" >&2
  exit 2
fi
echo "$@" >"$worktree_root/args.txt"
echo "Script executed at: $worktree_root" >"$worktree_root/script_output.txt"
