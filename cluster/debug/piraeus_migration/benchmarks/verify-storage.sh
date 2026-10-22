#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 TARGET_NAME" >&2
  exit 2
fi

target="$1"
namespace=piraeus-storage-bench-20261019
root=$(dirname "$0")
results="$root/results"
mkdir -p "$results"
case "$target" in
  piraeus-file-thin-one)
    claim=repo-piraeus-one
    group=piraeus-bench-file-thin-one-1mib-20261019
    expected_nodes=ovh-ns102453
    expected_count=1
    expected_peer_count=0
    ;;
  piraeus-file-thin-two-protocol-c)
    claim=repo-piraeus-two
    group=piraeus-bench-file-thin-two-1mib-20261019
    expected_nodes='ovh-ns102453 ovh-ns103656'
    expected_count=2
    expected_peer_count=1
    ;;
  *)
    exit 0
    ;;
esac

pv=$(kubectl -n "$namespace" get pvc "$claim" -o jsonpath='{.spec.volumeName}')
handle=$(kubectl get pv "$pv" -o jsonpath='{.spec.csi.volumeHandle}')
resource_table="$results/$target-placement.json"
kubectl -n piraeus-datastore exec deploy/linstor-controller -- \
  linstor --machine-readable resource list \
  | jq --arg r "$handle" \
    '[.[0][] | select(.name == $r) | {node_name, state, volumes: [.volumes[] | {storage_pool_name, disk_state: .state.disk_state, replication_states: [.state.replication_states[]?.replication_state]}]}]' \
    >"$resource_table"
if ! jq -e \
  --argjson expected_count "$expected_count" \
  --argjson expected_peer_count "$expected_peer_count" \
  --arg expected_nodes "$expected_nodes" \
  '
    (length == $expected_count)
    and ([.[].node_name] | sort == ($expected_nodes | split(" ") | sort))
    and all(.[]; (.volumes | length) == 1)
    and all(.[]; .volumes[0].storage_pool_name == "local-canary")
    and all(.[]; .volumes[0].disk_state == "UpToDate")
    and all(.[]; (.volumes[0].replication_states | length) == $expected_peer_count)
    and all(.[]; all(.volumes[0].replication_states[]; . == "Established"))
  ' "$resource_table" >/dev/null; then
  echo "$target does not have the expected fully UpToDate disk-backed placement" >&2
  cat "$resource_table" >&2
  exit 1
fi

# Inspect the active DRBD configuration on each intended resource node. Keep the
# command's output filtered because a full `drbdsetup show` can include secrets.
for node in $expected_nodes; do
  satellite=$(kubectl -n piraeus-datastore get pods -o json \
    | jq -r --arg node "$node" '
      [.items[] | select(.spec.nodeName == $node and (.metadata.name | startswith("linstor-satellite."))) | .metadata.name] | if length == 1 then .[0] else empty end
    ')
  if [ -z "$satellite" ]; then
    echo "$target has no unique LINSTOR satellite pod on $node" >&2
    exit 1
  fi
  expected_role=Secondary
  if [ "$node" = ovh-ns102453 ]; then
    expected_role=Primary
  fi
  role=$(kubectl -n piraeus-datastore exec "$satellite" -- drbdsetup role "$handle")
  if [ "$role" != "$expected_role" ]; then
    echo "$target role on $node is $role; expected $expected_role" >&2
    exit 1
  fi
  protocol_file="$results/$target-active-settings-$node.txt"
  kubectl -n piraeus-datastore exec "$satellite" -- drbdsetup show all --show-defaults \
    | awk -v r="$handle" '
      /^[[:space:]]*resource[[:space:]]/ { show = index($0, "\"" r "\"") > 0 }
      show && /(protocol|rs-discard-granularity)/ {
        line = $0
        sub(/#.*/, "", line)
        sub(/^[[:space:]]*/, "", line)
        gsub(/[[:space:]]+/, " ", line)
        sub(/[[:space:]]*$/, "", line)
        print line
      }
    ' >"$protocol_file"
  if [ "$(rg -c '^rs-discard-granularity 1048576;$' "$protocol_file")" != 1 ] \
    || { [ "$target" = piraeus-file-thin-two-protocol-c ] \
      && [ "$(rg -c '^protocol C;$' "$protocol_file")" != 1 ]; }; then
    echo "$target active DRBD settings on $node do not match the declared 1MiB granularity and protocol-C requirement" >&2
    cat "$protocol_file" >&2
    exit 1
  fi
done

properties="$results/$target-properties.txt"
kubectl -n piraeus-datastore exec deploy/linstor-controller -- \
  linstor --no-color resource-group list-properties "$group" \
  | rg -e 'DrbdOptions/Net/protocol' -e 'DrbdOptions/auto-diskful' \
    -e 'DrbdOptions/auto-rs-discard-granularity' \
    -e 'DrbdOptions/Disk/rs-discard-granularity' >"$properties"
if ! rg -q 'DrbdOptions/Net/protocol.*C' "$properties"; then
  echo "$target resource group does not specify protocol C" >&2
  cat "$properties" >&2
  exit 1
fi
if ! rg -q 'DrbdOptions/auto-rs-discard-granularity.*false' "$properties" \
  || ! rg -q 'DrbdOptions/Disk/rs-discard-granularity.*1048576' "$properties"; then
  echo "$target resource group does not declare fixed 1MiB discard granularity" >&2
  cat "$properties" >&2
  exit 1
fi
if [ "$target" = piraeus-file-thin-one ]; then
  if ! rg -q 'DrbdOptions/auto-diskful.*1' "$properties" \
    || ! rg -q 'DrbdOptions/auto-diskful-allow-cleanup.*true' "$properties"; then
    echo "$target resource group lacks auto-diskful=1 with cleanup enabled" >&2
    cat "$properties" >&2
    exit 1
  fi
fi
cat "$resource_table"
cat "$properties"
