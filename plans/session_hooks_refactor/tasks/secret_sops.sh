#!/usr/bin/env bash
# Usage: secret_sops.sh <ENV_VAR_NAME> <SOPS_FILE> <SOPS_PATH>
# Decrypts a single value and appends `export NAME=VALUE` to $ENV_OUT.
set -euo pipefail

name=$1
file=$2
sops_path=$3

if ! value=$(sops -d --extract "$sops_path" "$file" 2>/tmp/sops-err.$$); then
  echo "FAIL: sops decrypt $file $sops_path"
  cat /tmp/sops-err.$$ >&2
  rm -f /tmp/sops-err.$$
  exit 1
fi
rm -f /tmp/sops-err.$$

printf 'export %s=%q\n' "$name" "$value" >>"$ENV_OUT"
echo "OK: $name from $file"
