#!/bin/sh
# Keep /data a shallow checkout of haku-state so nginx can serve dashboard/.
# Read-only use of the haku git credential (the repo's own creds, delivered as
# the haku-state-git-write Secret; the budget Fava app reuses repo creds for its
# read-only sync the same way). Arg "once" syncs once and exits (init
# container); default loops.
set -eu

REPO="http://forgejo-http.forgejo:3000/haku/haku-state.git"
DIR=/data
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
