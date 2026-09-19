#!/usr/bin/env bash
set -euo pipefail

if (($# == 0)); then
  echo "usage: $0 <curl arguments...>" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
credential_file="$repo_root/cluster/k8s/oci-cache/talos-image-factory-credential.sops.yaml"

umask 077
netrc_file=$(mktemp)
trap 'rm -f "$netrc_file"' EXIT

# Keep the password out of curl's arguments and clean up the mode-600 file
# whether curl succeeds or fails.
sops -d "$credential_file" \
  | yq -r '"machine talos-image-factory.allegedly.works login " + .stringData.username + " password " + .stringData.password' \
    >"$netrc_file"

curl --netrc-file "$netrc_file" "$@"
