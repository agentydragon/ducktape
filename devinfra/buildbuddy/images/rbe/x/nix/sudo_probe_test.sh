#!/bin/bash
set -euo pipefail

id
sudo_path="$(command -v sudo || true)"
printf 'sudo_path=%s\n' "$sudo_path"
if [[ -n "$sudo_path" ]]; then
  ls -l "$sudo_path"
fi
if sudo -n true; then
  echo 'sudo_n_true=success'
else
  status=$?
  printf 'sudo_n_true=exit_%s\n' "$status"
fi
