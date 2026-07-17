#!/bin/sh
# GitLab app-path git bench, mirroring the Forgejo bench (forgejo-bench.sh):
# 20x each of API version / contents-read / contents-write, plus 20 tiny git pushes.
# Auth: PRIVATE-TOKEN header (API) + oauth2:<token> (git over HTTP).
set -eu

api="$GITLAB_URL/api/v4"
tok="$GITLAB_TOKEN"
proj="storage-bench-${TARGET_NAME}-$(date +%s)-$$"
work=/tmp/gitlab-storage-bench

req() {
  curl --fail-with-body -sS -H "PRIVATE-TOKEN: $tok" -H 'Content-Type: application/json' "$@"
}
timed() {
  op="$1"
  shift
  res="$(curl -sS -o /tmp/resp.json -w '%{http_code},%{time_total}' \
    -H "PRIVATE-TOKEN: $tok" -H 'Content-Type: application/json' "$@")"
  status="${res%%,*}"
  secs="${res#*,}"
  printf '%s,%s,%s,%s\n' "$TARGET_NAME" "$op" "$status" "$secs"
  case "$status" in 2*) ;; *)
    cat /tmp/resp.json >&2
    return 1
    ;;
  esac
}

cleanup() {
  [ -n "${pid:-}" ] && req -X DELETE "$api/projects/$pid" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# Create project with an initial commit so a default branch + README exist.
create="$(req -X POST "$api/projects" \
  --data "{\"name\":\"$proj\",\"visibility\":\"private\",\"initialize_with_readme\":true}")"
pid="$(printf '%s' "$create" | jq -r '.id')"
branch="$(printf '%s' "$create" | jq -r '.default_branch // "main"')"
path="$(printf '%s' "$create" | jq -r '.path_with_namespace')"
# Build the clone URL from the in-cluster endpoint (the API's http_url_to_repo carries
# the external hostname, which does not resolve in-cluster), with oauth2 token auth.
pushurl="$(printf '%s' "$GITLAB_URL" | sed "s#://#://oauth2:${tok}@#")/${path}.git"

# Wait for the repo to be importable (initialize_with_readme is async).
i=0
while [ "$i" -lt 30 ]; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H "PRIVATE-TOKEN: $tok" \
    "$api/projects/$pid/repository/files/README%2Emd?ref=$branch")"
  [ "$code" = "200" ] && break
  i=$((i + 1))
  sleep 1
done

# Warm each path once.
req "$api/version" >/dev/null
req "$api/projects/$pid/repository/files/README%2Emd?ref=$branch" >/dev/null

printf 'target,operation,http_status,seconds\n'

i=1
while [ "$i" -le 20 ]; do
  timed version "$api/version"
  i=$((i + 1))
done

i=1
while [ "$i" -le 20 ]; do
  timed contents_read "$api/projects/$pid/repository/files/README%2Emd?ref=$branch"
  i=$((i + 1))
done

i=1
while [ "$i" -le 20 ]; do
  timed contents_write -X POST "$api/projects/$pid/repository/files/api-write-$i%2Etxt" \
    --data "{\"branch\":\"$branch\",\"content\":\"contents write $i\",\"commit_message\":\"API write $i\"}"
  i=$((i + 1))
done

rm -rf "$work"
git clone --quiet "$pushurl" "$work"
git -C "$work" config user.name 'Gitlab benchmark'
git -C "$work" config user.email bench@invalid.example

i=1
while [ "$i" -le 20 ]; do
  printf 'git push %s\n' "$i" >"$work/git-write-$i.txt"
  git -C "$work" add "git-write-$i.txt"
  git -C "$work" commit --quiet -m "Git write $i"
  start="$(awk '{print $1}' /proc/uptime)"
  git -C "$work" push --quiet origin "HEAD:$branch"
  end="$(awk '{print $1}' /proc/uptime)"
  secs="$(awk -v s="$start" -v e="$end" 'BEGIN { printf "%.2f", e-s }')"
  printf '%s,git_push,200,%s\n' "$TARGET_NAME" "$secs"
  i=$((i + 1))
done
