#!/bin/sh
# Keep /ledger a plain, in-place working tree of the budget-ledger repo.
#
# Unlike git-sync (worktree-per-commit + symlink swap), this updates the same
# files in place so /ledger/main.beancount keeps a stable realpath. That matters
# because beancount canonicalises the loaded path at startup; if the file's
# realpath disappeared on each commit (as it does under git-sync's worktree GC),
# Fava would pin a deleted path and serve an empty ledger. In-place updates let
# Fava's file watcher see the mtime change and auto-reload fresh content.
#
# Arg: "once" syncs a single time and exits (init container); default loops.
set -eu

REPO="http://forgejo-http.forgejo:3000/budget-ledger/ledger.git"
DIR=/ledger
export HOME=/tmp

git config --global --replace-all credential.helper \
  '!f() { printf "username=%s\npassword=%s\n" "$GIT_USERNAME" "$GIT_PASSWORD"; }; f'
git config --global safe.directory "$DIR"

sync() {
  if [ -e "$DIR/.git" ]; then
    git -C "$DIR" fetch --depth 1 origin HEAD
    git -C "$DIR" reset --hard FETCH_HEAD
  else
    git clone --depth 1 "$REPO" "$DIR"
  fi
}

sync
[ "${1:-loop}" = "once" ] && exit 0

while true; do
  sleep "${SYNC_PERIOD:-300}"
  sync
done
