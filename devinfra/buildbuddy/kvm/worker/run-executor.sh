#!/usr/bin/env bash
set -euo pipefail

readonly key_file=/run/buildbuddy-key/api-key
readonly environment_dir=/run/buildbuddy-executor
readonly environment_file="$environment_dir/environment"
readonly executor_name=buildbuddy-kvm-worker-executor
readonly executor_image=buildbuddy.bbcr.io/public/buildbuddy-executor-enterprise@sha256:465d670159f7f2be756eb31815d7c6be54ce812de74a5cc49d3f6bda1fc7b078

if [[ ! -s "$key_file" ]]; then
  echo "BuildBuddy API key is missing from the mounted bbkey disk" >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$environment_dir"
umask 077
api_key="$(/usr/bin/tr -d '\r\n' <"$key_file")"
if [[ -z "$api_key" ]]; then
  echo "BuildBuddy API key is empty" >&2
  exit 1
fi
printf 'BUILDBUDDY_API_KEY=%s\n' "$api_key" >"$environment_file"
unset api_key

if /usr/bin/docker container inspect "$executor_name" >/dev/null 2>&1; then
  /usr/bin/docker container rm --force "$executor_name"
fi

exec /usr/bin/docker run \
  --rm \
  --name "$executor_name" \
  --pull=missing \
  --network host \
  --env "MY_HOSTNAME=$(hostname)" \
  --env MY_POOL=kvm-vm \
  --env-file "$environment_file" \
  --volume /etc/buildbuddy/executor.yaml:/config.yaml:ro \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume /buildbuddy:/buildbuddy \
  "$executor_image" \
  --config_file=/config.yaml \
  --server_type=buildbuddy-executor
