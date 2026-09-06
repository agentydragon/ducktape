#!/usr/bin/env bash
# Host network namespace only: ss does not enumerate pod network namespaces.
# Keep inode/socket identity, owner/cgroup and cumulative TCP counters together so
# adjacent snapshots expose activity on sockets opened before collection began.
set -euo pipefail

printf '%s event=snapshot-start host=%s boot_id=%s netns=%s\n' \
  "$(date -u '+%Y-%m-%dT%H:%M:%S.%NZ')" "$(uname -n)" \
  "$(</proc/sys/kernel/random/boot_id)" "$(readlink /proc/self/ns/net)"
ss --tcp --info --processes --numeric --extended --cgroup --no-header --oneline state established
printf '%s event=snapshot-end\n' "$(date -u '+%Y-%m-%dT%H:%M:%S.%NZ')"
