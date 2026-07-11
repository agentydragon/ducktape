#!/bin/sh
set -eu

auth="$(printf '%s:%s' "$FORGEJO_USERNAME" "$FORGEJO_PASSWORD" | base64 | tr -d '\n')"
repo="ceph-storage-bench"
api="$FORGEJO_URL/api/v1"
work=/tmp/forgejo-storage-bench

request() {
  curl --fail-with-body --silent --show-error \
    -H "Authorization: Basic $auth" \
    -H 'Content-Type: application/json' "$@"
}

timed_request() {
  operation="$1"
  shift
  result="$(curl --silent --show-error --output /tmp/response.json \
    --write-out '%{http_code},%{time_total}' \
    -H "Authorization: Basic $auth" \
    -H 'Content-Type: application/json' "$@")"
  status="${result%%,*}"
  seconds="${result#*,}"
  printf '%s,%s,%s,%s\n' "$TARGET_NAME" "$operation" "$status" "$seconds"
  case "$status" in
    2*) ;;
    *)
      cat /tmp/response.json >&2
      return 1
      ;;
  esac
}

cleanup() {
  request -X DELETE "$api/repos/$FORGEJO_USERNAME/$repo" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
cleanup

request -X POST "$api/user/repos" \
  --data "{\"name\":\"$repo\",\"private\":true,\"auto_init\":true,\"default_branch\":\"main\"}" \
  >/dev/null

seed="$(printf seed | base64 | tr -d '\n')"
request -X POST "$api/repos/$FORGEJO_USERNAME/$repo/contents/seed.txt" \
  --data "{\"content\":\"$seed\",\"message\":\"seed benchmark\"}" >/dev/null

printf 'target,operation,http_status,seconds\n'

# Warm each application path once before collecting samples.
request "$api/version" >/dev/null
request "$api/repos/$FORGEJO_USERNAME/$repo/contents/seed.txt" >/dev/null

i=1
while [ "$i" -le 20 ]; do
  timed_request version GET "$api/version"
  i=$((i + 1))
done

i=1
while [ "$i" -le 20 ]; do
  timed_request contents_read GET "$api/repos/$FORGEJO_USERNAME/$repo/contents/seed.txt"
  i=$((i + 1))
done

i=1
while [ "$i" -le 20 ]; do
  content="$(printf 'contents write %s\n' "$i" | base64 | tr -d '\n')"
  timed_request contents_write -X POST \
    "$api/repos/$FORGEJO_USERNAME/$repo/contents/api-write-$i.txt" \
    --data "{\"content\":\"$content\",\"message\":\"API write $i\"}"
  i=$((i + 1))
done

rm -rf "$work"
git -c http.extraHeader="Authorization: Basic $auth" clone --quiet \
  "$FORGEJO_URL/$FORGEJO_USERNAME/$repo.git" "$work"
git -C "$work" config user.name 'Ceph benchmark'
git -C "$work" config user.email bench@invalid.example

i=1
while [ "$i" -le 20 ]; do
  printf 'git push %s\n' "$i" >"$work/git-write-$i.txt"
  git -C "$work" add "git-write-$i.txt"
  git -C "$work" commit --quiet -m "Git write $i"
  start="$(awk '{print $1}' /proc/uptime)"
  git -c http.extraHeader="Authorization: Basic $auth" -C "$work" push --quiet origin HEAD:main
  end="$(awk '{print $1}' /proc/uptime)"
  seconds="$(awk -v start="$start" -v end="$end" 'BEGIN { printf "%.2f", end-start }')"
  printf '%s,git_push,200,%s\n' "$TARGET_NAME" "$seconds"
  i=$((i + 1))
done
