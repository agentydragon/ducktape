#!/usr/bin/env bash
set -euo pipefail

readonly key_file=/run/buildbuddy-key/api-key
readonly config_dir=/run/buildbuddy-config
readonly pool_file="$config_dir/pool"
readonly environment_dir=/run/buildbuddy-executor
readonly environment_file="$environment_dir/environment"
readonly executor_name=buildbuddy-kvm-worker-executor

: "${BUILDBUDDY_EXECUTOR_IMAGE:?executor image tag is not configured}"

for path in /buildbuddy /run/buildbuddy-key "$config_dir"; do
  mountpoint --quiet "$path"
done

if [[ ! -s "$key_file" ]]; then
  echo "BuildBuddy API key is missing from the mounted bbkey disk" >&2
  exit 1
fi

if [[ ! -s "$config_dir/executor.yaml" ]]; then
  echo "BuildBuddy executor config is missing from the mounted bbconfig disk" >&2
  exit 1
fi

if [[ ! -s "$pool_file" ]]; then
  echo "BuildBuddy pool override is missing from the mounted bbconfig disk" >&2
  exit 1
fi

mapfile -t pool_lines <"$pool_file"
if [[ "${#pool_lines[@]}" -ne 1 || -z "${pool_lines[0]}" ]]; then
  echo "BuildBuddy pool override must contain exactly one non-empty line" >&2
  exit 1
fi
readonly pool="${pool_lines[0]}"

readonly hostname="$(tr '[:upper:]' '[:lower:]' </sys/class/dmi/id/product_uuid)"
if [[ -z "$hostname" || "$hostname" == 00000000-0000-0000-0000-000000000000 ]]; then
  echo "KubeVirt did not provide a unique SMBIOS product UUID" >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$environment_dir"
umask 077
api_key="$(tr -d '\r\n' <"$key_file")"
if [[ -z "$api_key" ]]; then
  echo "BuildBuddy API key is empty" >&2
  exit 1
fi
printf 'BUILDBUDDY_API_KEY=%s\n' "$api_key" >"$environment_file"
unset api_key

if docker container inspect "$executor_name" >/dev/null 2>&1; then
  docker container rm --force "$executor_name"
fi

exec docker run \
  --rm \
  --name "$executor_name" \
  --pull=never \
  --network host \
  --env "MY_HOSTNAME=$hostname" \
  --env "MY_POOL=$pool" \
  --env-file "$environment_file" \
  --volume "$config_dir/executor.yaml:/config.yaml:ro" \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume /buildbuddy:/buildbuddy \
  --volume /etc/rancher/k3s:/etc/rancher/k3s:ro \
  "$BUILDBUDDY_EXECUTOR_IMAGE" \
  --config_file=/config.yaml \
  --server_type=buildbuddy-executor
