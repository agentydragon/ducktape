#!/usr/bin/env bash
set -euo pipefail

namespace=gitlab-spike
statefulset=gitlab-spike-gitaly-default
fault_key=gitlab-spike.ducktape.io/fault
policy=gitlab-spike-fault-isolation
if [[ ${ALLOW_GITLAB_SPIKE_FAILURE:-} != YES || ${BENCH_TARGET:-} != spike/acceptance ]]; then
  echo 'refusing failure injection: require ALLOW_GITLAB_SPIKE_FAILURE=YES and BENCH_TARGET=spike/acceptance' >&2
  exit 2
fi
case ${GIT_REMOTE_URL:-} in
  'https://gitlab-spike.allegedly.works/spike/acceptance.git' | \
    'http://gitlab-spike-webservice-default.gitlab-spike.svc:8181/spike/acceptance.git') ;;
  *)
    echo 'GIT_REMOTE_URL must be the exact spike/acceptance project URL without embedded credentials' >&2
    exit 2
    ;;
esac
for variable in GITALY_PRIMARY_POD PRAEFECT_PRIMARY_STORAGE PRAEFECT_POD TOOLBOX_POD \
  GITLAB_PROJECT_ID PRAEFECT_RELATIVE_PATH GIT_AUTH_USER GIT_AUTH_TOKEN; do
  if [[ -z ${!variable:-} ]]; then
    echo "$variable is required" >&2
    exit 2
  fi
done
command -v timeout >/dev/null || {
  echo 'GNU timeout is required to keep Git push retries within the outage window' >&2
  exit 2
}
if [[ $PRAEFECT_PRIMARY_STORAGE != "$GITALY_PRIMARY_POD" ]]; then
  echo 'primary storage must exactly match the operator-identified primary Gitaly pod name' >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../../../.." && pwd)
evidence_dir=${EVIDENCE_DIR:-/tmp/gitlab-spike-primary-failure-$(date -u +%Y%m%dT%H%M%SZ)}
case "$evidence_dir" in
  /*) ;;
  *)
    echo 'EVIDENCE_DIR must be an absolute path outside the repository' >&2
    exit 2
    ;;
esac
case "$evidence_dir/" in
  "$repo_root/"*)
    echo 'EVIDENCE_DIR cannot be inside the repository' >&2
    exit 2
    ;;
esac
umask 077
mkdir -p "$evidence_dir"
scratch=$(mktemp -d /tmp/gitlab-spike-primary-failure.XXXXXX)
fault_touched=0
primary_uid=
original_fault_label=

restore_fault_label() {
  [[ $fault_touched == 1 ]] || return 0
  local current_json current_uid current_fault
  if ! current_json=$(capture_resource pod "$GITALY_PRIMARY_POD" 2>/dev/null); then
    echo "could not verify whether the primary or its replacement still has the isolation label: $GITALY_PRIMARY_POD" >&2
    return 1
  fi
  current_uid=$(printf '%s\n' "$current_json" | jq -r '.metadata.uid')
  if [[ $current_uid != "$primary_uid" ]]; then
    echo 'primary pod was replaced during the exercise; leaving the replacement untouched and requiring manual label inspection' >&2
    fault_touched=0
    return 1
  fi
  current_fault=$(printf '%s\n' "$current_json" | jq -r --arg key "$fault_key" '.metadata.labels[$key] // empty')
  if [[ $current_fault == isolated ]]; then
    guarded_fault_label_change "$original_fault_label" isolated "$evidence_dir/unisolate-primary.json" || return 1
  elif [[ $current_fault != "$original_fault_label" ]]; then
    echo "primary fault label changed unexpectedly to '$current_fault'; refusing to overwrite it" >&2
    return 1
  fi
  fault_touched=0
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM
  if ! restore_fault_label; then
    echo "MANUAL RECOVERY: inspect $namespace/$GITALY_PRIMARY_POD; remove $fault_key=isolated only if its UID matches the recorded primary UID ($primary_uid), and verify a replacement Pod is unlabelled" >&2
    status=1
  fi
  rm -rf "$scratch"
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

capture_resource() {
  local resource=$1 name=$2
  timeout --kill-after=1s 20s kubectl --request-timeout=20s \
    -n "$namespace" get "$resource" "$name" -o json
}

guarded_fault_label_change() {
  local desired_value=$1 expected_value=$2 evidence_file=$3 require_ready=${4:-no}
  local attempt current_json current_uid current_fault current_resource_version patch label_path patched_json patch_stderr

  label_path=$(jq -nr --arg key "$fault_key" '"/metadata/labels/" + ($key | gsub("~"; "~0") | gsub("/"; "~1"))')
  for attempt in 1 2 3; do
    current_json=$(capture_resource pod "$GITALY_PRIMARY_POD") || return 1
    current_uid=$(printf '%s\n' "$current_json" | jq -r '.metadata.uid')
    if [[ $current_uid != "$primary_uid" ]]; then
      echo 'primary pod identity changed before the guarded label patch; refusing to touch the replacement' >&2
      return 1
    fi
    current_fault=$(printf '%s\n' "$current_json" | jq -r --arg key "$fault_key" '.metadata.labels[$key] // empty')
    if [[ $current_fault == "$desired_value" ]]; then
      printf '%s\n' "$current_json" | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace,uid:.metadata.uid,resourceVersion:.metadata.resourceVersion,labels:.metadata.labels}}' >"$evidence_file"
      return 0
    fi
    if [[ $current_fault != "$expected_value" ]]; then
      echo "primary fault label changed unexpectedly to '$current_fault'; expected '$expected_value'" >&2
      return 1
    fi
    current_resource_version=$(printf '%s\n' "$current_json" | jq -r '.metadata.resourceVersion // empty')
    if [[ -z $current_resource_version ]]; then
      echo 'primary Pod has no resourceVersion; refusing an unguarded fault-label patch' >&2
      return 1
    fi
    if [[ $require_ready == yes ]]; then
      printf '%s\n' "$current_json" | jq -e --arg name "$GITALY_PRIMARY_POD" --arg uid "$primary_uid" --arg sts "$statefulset" '
        .metadata.name == $name and .metadata.uid == $uid and .metadata.deletionTimestamp == null and
        .status.phase == "Running" and (.status.containerStatuses | length) > 0 and
        all(.status.containerStatuses[]; .ready == true) and
        any(.metadata.ownerReferences[]?; .controller == true and .kind == "StatefulSet" and .name == $sts)
      ' >/dev/null || {
        echo 'primary Pod is no longer ready and owned by the spike Gitaly StatefulSet before isolation' >&2
        return 1
      }
    fi

    patch=$(jq -cn --arg uid "$primary_uid" --arg rv "$current_resource_version" --arg path "$label_path" \
      --arg desired "$desired_value" '
        [
          {op:"test",path:"/metadata/uid",value:$uid},
          {op:"test",path:"/metadata/resourceVersion",value:$rv},
          (if $desired == "" then
             {op:"remove",path:$path}
           else
             {op:"add",path:$path,value:$desired}
           end)
        ]')
    patch_stderr="${evidence_file}.stderr"
    if patched_json=$(kubectl --request-timeout=20s -n "$namespace" patch pod "$GITALY_PRIMARY_POD" \
      --type=json --patch "$patch" -o json 2>"$patch_stderr"); then
      printf '%s\n' "$patched_json" | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace,uid:.metadata.uid,resourceVersion:.metadata.resourceVersion,labels:.metadata.labels}}' >"$evidence_file"
      printf '%s\n' "$patched_json" | jq -e --arg uid "$primary_uid" --arg key "$fault_key" --arg value "$desired_value" '
        .metadata.uid == $uid and (.metadata.labels[$key] // "") == $value
      ' >/dev/null || {
        echo 'guarded fault-label patch response did not confirm the expected Pod identity and label' >&2
        return 1
      }
      return 0
    fi
    if ((attempt < 3)); then
      sleep 1
    fi
  done

  cat "$patch_stderr" >&2
  echo 'guarded fault-label patch failed after three resourceVersion-checked attempts' >&2
  return 1
}

gitaly_tcp_probe() {
  local host=$1 port=$2 result_file=$3 status
  if timeout --kill-after=1s 12s kubectl --request-timeout=10s -n "$namespace" \
    exec "$TOOLBOX_POD" -c toolbox -- ruby -rtimeout -rsocket -e '
      host, port = ARGV
      begin
        socket = Timeout.timeout(3) { TCPSocket.new(host, Integer(port)) }
        socket.close
        puts "cilium_probe=connected"
        exit 0
      rescue Timeout::Error, Errno::ETIMEDOUT
        puts "cilium_probe=timeout"
        exit 42
      rescue StandardError => error
        warn "cilium_probe_error=#{error.class}: #{error.message}"
        exit 1
      end
    ' -- "$host" "$port" >"$result_file" 2>&1; then
    status=0
  else
    status=$?
  fi
  printf 'kubectl_exec_exit_status=%s\n' "$status" >>"$result_file"
  return "$status"
}

cilium_agent_endpoint_status() {
  local pod_json=$1 node_name pod_name pod_ip agent_pods agent_pod endpoint_map endpoint_id endpoint_json
  node_name=$(printf '%s\n' "$pod_json" | jq -er '.spec.nodeName | select(type == "string" and length > 0)') || return 1
  pod_name=$(printf '%s\n' "$pod_json" | jq -er '.metadata.name') || return 1
  pod_ip=$(printf '%s\n' "$pod_json" | jq -er '.status.podIP | select(type == "string" and length > 0)') || return 1

  agent_pods=$(timeout --kill-after=1s 15s kubectl --request-timeout=15s -n kube-system \
    get pods -l k8s-app=cilium --field-selector "spec.nodeName=$node_name" -o json) || return 1
  agent_pod=$(printf '%s\n' "$agent_pods" | jq -er '
    [.items[] | select(
      .status.phase == "Running" and
      any(.spec.containers[]?; .name == "cilium-agent") and
      any(.status.containerStatuses[]?; .name == "cilium-agent" and .ready == true)
    )]
    | if length == 1 then .[0].metadata.name else error("expected one ready Cilium agent on the endpoint node") end
  ') || return 1

  endpoint_map=$(timeout --kill-after=1s 20s kubectl --request-timeout=20s -n kube-system \
    exec "$agent_pod" -c cilium-agent -- cilium-dbg endpoint list \
    --output 'jsonpath={range [*]}{.id}{"\t"}{.status.networking.addressing[0].ipv4}{"\n"}{end}' \
    | awk -F '\t' -v address="$pod_ip" '$2 == address { print $1 }') || return 1
  local -a endpoint_ids=()
  mapfile -t endpoint_ids <<<"$endpoint_map"
  if ((${#endpoint_ids[@]} != 1)) || [[ -z ${endpoint_ids[0]:-} ]]; then
    echo "Cilium agent on $node_name did not return exactly one endpoint for $namespace/$pod_name at $pod_ip" >&2
    return 1
  fi
  endpoint_id=${endpoint_ids[0]}

  endpoint_json=$(timeout --kill-after=1s 20s kubectl --request-timeout=20s -n kube-system \
    exec "$agent_pod" -c cilium-agent -- cilium-dbg endpoint get "$endpoint_id" --output json) || return 1
  printf '%s\n' "$endpoint_json" | jq -ce --arg ns "$namespace" --arg pod "$pod_name" --arg ip "$pod_ip" '
    if length != 1 then error("expected exactly one Cilium endpoint from endpoint get") else .[0] end |
    select(
      .status["external-identifiers"]["k8s-namespace"] == $ns and
      .status["external-identifiers"]["k8s-pod-name"] == $pod and
      any(.status.networking.addressing[]?; .ipv4 == $ip)
    )
  '
}

wait_for_cilium_isolation() {
  local deadline current_pod_json endpoint_json cilium_endpoint_json probe_status policy_enabled current_peer_json peer_endpoint_json
  deadline=$(($(date -u +%s) + 30))
  while (($(date -u +%s) < deadline)); do
    current_pod_json=$(capture_resource pod "$GITALY_PRIMARY_POD") || return 1
    printf '%s\n' "$current_pod_json" | jq -e --arg uid "$primary_uid" '
      .metadata.uid == $uid and .status.phase == "Running"
    ' >/dev/null || {
      echo 'primary Pod was replaced or stopped running while waiting for Cilium isolation' >&2
      return 1
    }

    if cilium_endpoint_json=$(capture_resource ciliumendpoints.cilium.io "$GITALY_PRIMARY_POD" 2>/dev/null) \
      && endpoint_json=$(cilium_agent_endpoint_status "$current_pod_json" 2>/dev/null) \
      && printf '%s\n' "$cilium_endpoint_json" | jq -e --arg uid "$primary_uid" --arg key "$fault_key" '
        any(.metadata.ownerReferences[]?; .kind == "Pod" and .uid == $uid) and
        .metadata.labels.app == "gitaly" and .metadata.labels.release == "gitlab-spike" and
        .metadata.labels[$key] == "isolated" and .status.state == "ready"
      ' >/dev/null \
      && printf '%s\n' "$endpoint_json" | jq -e --arg key "$fault_key" '
        any(.status.identity.labels[]?; . == "k8s:app=gitaly") and
        any(.status.identity.labels[]?; . == "k8s:release=gitlab-spike") and
        any(.status.identity.labels[]?; . == ("k8s:" + $key + "=isolated")) and
        .status.state == "ready" and .status.policy.realized["policy-enabled"] == "both"
      ' >/dev/null; then
      current_pod_json=$(capture_resource pod "$GITALY_PRIMARY_POD") || return 1
      printf '%s\n' "$current_pod_json" | jq -e --arg uid "$primary_uid" '
        .metadata.uid == $uid and .status.phase == "Running"
      ' >/dev/null || {
        echo 'primary Pod was replaced or stopped running while validating Cilium enforcement' >&2
        return 1
      }
      printf '%s\n' "$endpoint_json" | jq -S '{id,externalIdentifiers:.status["external-identifiers"],networking:.status.networking.addressing,status:{state:.status.state,identity:.status.identity,policy:.status.policy,health:.status.health}}' \
        >"$evidence_dir/cilium-endpoint-isolated.json"
      printf '%s\n' "$cilium_endpoint_json" | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace,ownerReferences:.metadata.ownerReferences,labels:.metadata.labels},status:{state:.status.state,identity:.status.identity,networking:.status.networking}}' \
        >"$evidence_dir/kubernetes-cilium-endpoint-isolated.json"
      policy_enabled=$(printf '%s\n' "$endpoint_json" | jq -r '.status.policy.realized["policy-enabled"]')
      if gitaly_tcp_probe "$gitaly_primary_ip" "$gitaly_grpc_port" "$evidence_dir/gitaly-probe-during-fault.txt"; then
        echo 'Gitaly TCP connect still succeeds after the Cilium agent reports both-direction policy enforcement' >&2
        return 1
      else
        probe_status=$?
      fi
      if [[ $probe_status != 42 ]] || ! grep -qx 'cilium_probe=timeout' "$evidence_dir/gitaly-probe-during-fault.txt"; then
        echo 'Gitaly read-only probe failed for a reason other than a TCP timeout; refusing to treat it as Cilium enforcement' >&2
        return 1
      fi

      current_peer_json=$(capture_resource pod "$healthy_peer_pod") || return 1
      printf '%s\n' "$current_peer_json" | jq -e --arg uid "$healthy_peer_uid" --arg key "$fault_key" '
        .metadata.uid == $uid and .metadata.deletionTimestamp == null and .status.phase == "Running" and
        (.status.containerStatuses | length) > 0 and all(.status.containerStatuses[]; .ready == true) and
        (.metadata.labels[$key] // "") != "isolated"
      ' >/dev/null || {
        echo 'healthy Gitaly peer changed identity or stopped being ready during the isolation probe' >&2
        return 1
      }
      peer_endpoint_json=$(cilium_agent_endpoint_status "$current_peer_json") || {
        echo 'could not inspect the healthy Gitaly peer endpoint on its Cilium agent' >&2
        return 1
      }
      printf '%s\n' "$peer_endpoint_json" | jq -e '.status.state == "ready"' >/dev/null || {
        echo 'healthy Gitaly peer endpoint is not ready during the isolation probe' >&2
        return 1
      }
      if ! gitaly_tcp_probe "$healthy_peer_ip" "$healthy_peer_grpc_port" "$evidence_dir/gitaly-peer-probe-during-fault.txt" \
        || ! grep -qx 'cilium_probe=connected' "$evidence_dir/gitaly-peer-probe-during-fault.txt"; then
        echo 'healthy Gitaly peer read-only positive-control probe did not connect during primary isolation' >&2
        return 1
      fi
      printf '%s\n' "$peer_endpoint_json" | jq -S '{id,externalIdentifiers:.status["external-identifiers"],networking:.status.networking.addressing,status:{state:.status.state,identity:.status.identity,policy:.status.policy,health:.status.health}}' \
        >"$evidence_dir/cilium-endpoint-peer-during-fault.json"
      printf 'endpoint_state=ready\npolicy_enabled=%s\nprimary_probe=timeout\nhealthy_peer_probe=connected\n' "$policy_enabled" \
        >"$evidence_dir/cilium-isolation-proof.txt"
      return 0
    fi
    sleep 2
  done
  echo 'Cilium agent endpoint did not report the isolated identity, ready state and realized ingress plus egress enforcement within 30 seconds' >&2
  return 1
}

praefect_check() {
  timeout --kill-after=1s 20s kubectl --request-timeout=20s \
    -n "$namespace" exec "$PRAEFECT_POD" -c praefect -- \
    /usr/local/bin/praefect -config /etc/gitaly/config.toml check
}

praefect_metadata() {
  timeout --kill-after=1s 20s kubectl --request-timeout=20s \
    -n "$namespace" exec "$PRAEFECT_POD" -c praefect -- \
    /usr/local/bin/praefect -config /etc/gitaly/config.toml metadata \
    -virtual-storage default -relative-path "$PRAEFECT_RELATIVE_PATH"
}

replica_checksums() {
  timeout --kill-after=1s 60s kubectl --request-timeout=60s \
    -n "$namespace" exec "$TOOLBOX_POD" -c toolbox -- \
    gitlab-rake "gitlab:praefect:replicas[$GITLAB_PROJECT_ID]"
}

metadata_primary() {
  awk -F: '
    /^[[:space:]]*Primary[[:space:]]*:/ {
      value = $2
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      gsub(/^\"|\"$/, "", value)
      print value
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "$1"
}

assert_all_replicas_current() {
  local metadata_file=$1
  awk -F: '
    /^[[:space:]]*-?[[:space:]]*Storage[[:space:]]*:/ { storages++ }
    /^[[:space:]]*Healthy[[:space:]]*:[[:space:]]*true[[:space:]]*$/ { healthy++ }
    /^[[:space:]]*Generation[[:space:]]*:/ {
      generation = $2
      sub(/^[[:space:]]+/, "", generation)
      sub(/[^0-9].*$/, "", generation)
      if (generations == 0) expected = generation
      if (generation == "" || generation != expected) mismatch = 1
      generations++
    }
    END { if (storages != 3 || healthy != 3 || generations != 4 || mismatch) exit 1 }
  ' "$metadata_file" || {
    echo "expected three healthy replicas at the repository generation in $metadata_file" >&2
    return 1
  }
}

wait_for_current_replicas() {
  local metadata_file=$1 attempt=0 ready deadline
  deadline=$(($(date -u +%s) + 180))
  while (($(date -u +%s) < deadline)); do
    ((attempt += 1))
    ready=$(timeout --kill-after=1s 20s kubectl --request-timeout=20s \
      -n "$namespace" get statefulset "$statefulset" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
    ready=${ready:-0}
    if [[ $ready == 3 ]] && praefect_metadata >"$metadata_file" 2>&1 \
      && assert_all_replicas_current "$metadata_file" >/dev/null 2>&1; then
      printf 'replica_catchup_attempts=%s\n' "$attempt" >>"$evidence_dir/write.txt"
      return 0
    fi
    sleep 2
  done
  echo 'three healthy Praefect replicas did not reach the repository generation before the bounded deadline' >&2
  return 1
}

assert_replica_checksums_match() {
  local checksum_file=$1
  awk -F '|' '
    function trim(value) {
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      return value
    }
    /Something went wrong when getting replicas/ { task_error = 1 }
    index($0, "Project name") == 1 && index($0, "|") > 0 { table_row = 1; next }
    table_row == 1 { table_row = 2; next }
    table_row == 2 {
      fields = NF
      if (trim($1) == "Acceptance") {
        rows++
        if (fields < 4) mismatch = 1
        for (i = 2; i <= fields; i++) {
          value = trim($i)
          if (value == "") mismatch = 1
          if (i == 2) expected = value
          else if (value != expected) mismatch = 1
        }
        checksum_count = fields - 1
      }
      table_row = 0
    }
    END {
      if (task_error || rows != 1 || checksum_count < 3 || mismatch) exit 1
      print "replicas=" checksum_count " checksum=" expected
    }
  ' "$checksum_file" >"$checksum_file.validated" || {
    echo "Praefect checksum report does not show one acceptance row with at least three equal replica checksums: $checksum_file" >&2
    return 1
  }
}

metadata_file_primary() {
  local metadata_file=$1 actual
  actual=$(metadata_primary "$metadata_file") || {
    echo "could not read Primary field from $metadata_file" >&2
    return 1
  }
  printf '%s\n' "$actual"
}

wait_for_promotion() {
  local attempt=0 current deadline
  deadline=$(($(date -u +%s) + 180))
  while (($(date -u +%s) < deadline)); do
    ((attempt += 1))
    if praefect_metadata >"$evidence_dir/praefect-metadata-during-fault.txt" 2>&1; then
      current=$(metadata_primary "$evidence_dir/praefect-metadata-during-fault.txt" || true)
      if [[ -n $current && $current != "$PRAEFECT_PRIMARY_STORAGE" ]]; then
        printf '%s\n' "$current" >"$evidence_dir/promoted-storage.txt"
        return 0
      fi
    fi
    sleep 2
  done
  echo 'Praefect did not report a different primary during the bounded failure window' >&2
  return 1
}

validate_refs() {
  local before=$1 after=$2
  awk 'NR==FNR {before[$2]=$1; next} {after[$2]=$1} END {for (ref in before) if (!(ref in after) || after[ref] != before[ref]) exit 1}' \
    "$before" "$after" || {
    echo 'a pre-existing repository ref changed or disappeared' >&2
    return 1
  }
}

sts_json=$(capture_resource statefulset "$statefulset")
printf '%s\n' "$sts_json" | jq -e '
  .metadata.namespace == "gitlab-spike" and
  .metadata.name == "gitlab-spike-gitaly-default" and
  .metadata.labels.release == "gitlab-spike" and
  .spec.replicas == 3 and .status.readyReplicas == 3
' >/dev/null || {
  echo 'refusing injection: named spike Gitaly StatefulSet must have exactly three ready replicas' >&2
  exit 2
}

primary_json=$(capture_resource pod "$GITALY_PRIMARY_POD")
printf '%s\n' "$primary_json" | jq -e --arg name "$GITALY_PRIMARY_POD" --arg sts "$statefulset" --arg key "$fault_key" '
  .metadata.namespace == "gitlab-spike" and .metadata.name == $name and
  .metadata.labels.release == "gitlab-spike" and .metadata.labels.app == "gitaly" and
  any(.metadata.ownerReferences[]?; .controller == true and .kind == "StatefulSet" and .name == $sts) and
  .status.phase == "Running" and (.status.containerStatuses | length) > 0 and
  all(.status.containerStatuses[]; .ready == true) and
  (.metadata.labels[$key] // "") != "isolated"
' >/dev/null || {
  echo 'refusing injection: supplied primary is not a ready, unisolated pod owned by the spike Gitaly StatefulSet' >&2
  exit 2
}
primary_uid=$(printf '%s\n' "$primary_json" | jq -r '.metadata.uid')
original_fault_label=$(printf '%s\n' "$primary_json" | jq -r --arg key "$fault_key" '.metadata.labels[$key] // empty')
gitaly_primary_ip=$(printf '%s\n' "$primary_json" | jq -er '.status.podIP | select(type == "string" and length > 0)')
gitaly_grpc_port=$(printf '%s\n' "$primary_json" | jq -er '
  [.spec.containers[] | select(.name == "gitaly") | .ports[]? | select(.name == "grpc-gitaly") | .containerPort]
  | if length == 1 and (.[0] | type == "number" and . > 0 and . <= 65535) then .[0] else error("expected one valid grpc-gitaly container port") end
')
healthy_peer_json=$(kubectl -n "$namespace" get pods -l app=gitaly,release=gitlab-spike -o json | jq -cer \
  --arg primary_uid "$primary_uid" --arg sts "$statefulset" --arg key "$fault_key" '
    [.items[] | select(
      .metadata.uid != $primary_uid and .metadata.deletionTimestamp == null and
      .metadata.labels.app == "gitaly" and .metadata.labels.release == "gitlab-spike" and
      (.metadata.labels[$key] // "") != "isolated" and .status.phase == "Running" and
      (.status.containerStatuses | length) > 0 and all(.status.containerStatuses[]; .ready == true) and
      any(.metadata.ownerReferences[]?; .controller == true and .kind == "StatefulSet" and .name == $sts)
    )]
    | sort_by(.metadata.name)
    | if length > 0 then .[0] else error("no ready unisolated Gitaly peer exists") end
  ')
healthy_peer_pod=$(printf '%s\n' "$healthy_peer_json" | jq -er '.metadata.name')
healthy_peer_uid=$(printf '%s\n' "$healthy_peer_json" | jq -er '.metadata.uid')
healthy_peer_ip=$(printf '%s\n' "$healthy_peer_json" | jq -er '.status.podIP | select(type == "string" and length > 0)')
healthy_peer_grpc_port=$(printf '%s\n' "$healthy_peer_json" | jq -er '
  [.spec.containers[] | select(.name == "gitaly") | .ports[]? | select(.name == "grpc-gitaly") | .containerPort]
  | if length == 1 and (.[0] | type == "number" and . > 0 and . <= 65535) then .[0] else error("expected one valid grpc-gitaly container port") end
')

for pod_spec in "$PRAEFECT_POD:praefect" "$TOOLBOX_POD:toolbox"; do
  pod=${pod_spec%%:*}
  app=${pod_spec#*:}
  pod_json=$(capture_resource pod "$pod")
  printf '%s\n' "$pod_json" | jq -e --arg pod "$pod" --arg app "$app" '
    .metadata.namespace == "gitlab-spike" and .metadata.name == $pod and
    .metadata.labels.release == "gitlab-spike" and .metadata.labels.app == $app and
    .status.phase == "Running" and (.status.containerStatuses | length) > 0 and
    all(.status.containerStatuses[]; .ready == true)
  ' >/dev/null || {
    echo "refusing injection: $pod is not a ready spike $app pod" >&2
    exit 2
  }
done

policy_json=$(kubectl -n "$namespace" get ciliumnetworkpolicies.cilium.io "$policy" -o json)
printf '%s\n' "$policy_json" | jq -e --arg key "$fault_key" '
  .metadata.namespace == "gitlab-spike" and .metadata.name == "gitlab-spike-fault-isolation" and
  .spec.endpointSelector.matchLabels.app == "gitaly" and
  .spec.endpointSelector.matchLabels.release == "gitlab-spike" and
  .spec.endpointSelector.matchLabels[$key] == "isolated" and
  any(.spec.ingressDeny[]?.fromEntities[]?; . == "all") and
  any(.spec.egressDeny[]?.toEntities[]?; . == "all")
' >/dev/null || {
  echo 'refusing injection: generated Cilium policy is absent or does not deny all traffic for the exact isolated Gitaly selector' >&2
  exit 2
}
initially_isolated=$(kubectl -n "$namespace" get pods \
  -l "app=gitaly,release=gitlab-spike,$fault_key=isolated" -o json | jq '.items | length')
if [[ $initially_isolated != 0 ]]; then
  echo 'refusing injection: another Gitaly pod already has the isolation label' >&2
  exit 2
fi

context=$(kubectl config current-context)
printf 'started_at_utc=%s\ncontext=%s\nnamespace=%s\nstatefulset=%s\nprimary_pod=%s\nprimary_storage=%s\nhealthy_peer_pod=%s\nhealthy_peer_uid=%s\npraefect_pod=%s\ntoolbox_pod=%s\nproject=spike/acceptance\nproject_id=%s\nrelative_path=%s\nremote=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$context" "$namespace" "$statefulset" \
  "$GITALY_PRIMARY_POD" "$PRAEFECT_PRIMARY_STORAGE" "$healthy_peer_pod" "$healthy_peer_uid" \
  "$PRAEFECT_POD" "$TOOLBOX_POD" \
  "$GITLAB_PROJECT_ID" "$PRAEFECT_RELATIVE_PATH" "$GIT_REMOTE_URL" >"$evidence_dir/metadata.txt"
printf '%s\n' "$sts_json" | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace,labels:.metadata.labels},spec:{replicas:.spec.replicas,selector:.spec.selector},status:{readyReplicas:.status.readyReplicas}}' \
  >"$evidence_dir/statefulset-before.json"
printf '%s\n' "$policy_json" | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace},spec:.spec}' \
  >"$evidence_dir/fault-policy.json"
kubectl -n "$namespace" get pods -o wide >"$evidence_dir/pods-before.txt"
praefect_check >"$evidence_dir/praefect-check-before.txt" 2>&1
praefect_metadata >"$evidence_dir/praefect-metadata-before.txt" 2>&1
baseline_primary=$(metadata_file_primary "$evidence_dir/praefect-metadata-before.txt")
if [[ $baseline_primary != "$PRAEFECT_PRIMARY_STORAGE" ]]; then
  echo "refusing injection: metadata primary $baseline_primary does not match supplied target $PRAEFECT_PRIMARY_STORAGE" >&2
  exit 2
fi
assert_all_replicas_current "$evidence_dir/praefect-metadata-before.txt"
replica_checksums >"$evidence_dir/replica-checksums-before.txt" 2>&1
assert_replica_checksums_match "$evidence_dir/replica-checksums-before.txt"
baseline_cilium_endpoint=$(capture_resource ciliumendpoints.cilium.io "$GITALY_PRIMARY_POD")
printf '%s\n' "$baseline_cilium_endpoint" | jq -e --arg uid "$primary_uid" '
  any(.metadata.ownerReferences[]?; .kind == "Pod" and .uid == $uid) and .status.state == "ready"
' >/dev/null || {
  echo 'refusing injection: the primary has no ready CiliumEndpoint status for its current Pod UID' >&2
  exit 2
}
baseline_agent_endpoint=$(cilium_agent_endpoint_status "$primary_json") || {
  echo 'refusing injection: cannot inspect the primary endpoint through its node-local Cilium agent' >&2
  exit 2
}
printf '%s\n' "$baseline_agent_endpoint" | jq -e '
  any(.status.identity.labels[]?; . == "k8s:app=gitaly") and
  any(.status.identity.labels[]?; . == "k8s:release=gitlab-spike") and .status.state == "ready"
' >/dev/null || {
  echo 'refusing injection: node-local Cilium agent endpoint does not match the current Gitaly pod identity and ready state' >&2
  exit 2
}
baseline_peer_endpoint=$(cilium_agent_endpoint_status "$healthy_peer_json") || {
  echo 'refusing injection: cannot inspect the healthy peer endpoint through its node-local Cilium agent' >&2
  exit 2
}
printf '%s\n' "$baseline_peer_endpoint" | jq -e '
  any(.status.identity.labels[]?; . == "k8s:app=gitaly") and
  any(.status.identity.labels[]?; . == "k8s:release=gitlab-spike") and .status.state == "ready"
' >/dev/null || {
  echo 'refusing injection: healthy peer endpoint is not ready with the expected Gitaly identity' >&2
  exit 2
}
printf '%s\n' "$baseline_cilium_endpoint" | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace,labels:.metadata.labels,ownerReferences:.metadata.ownerReferences},status:{state:.status.state,identity:.status.identity,networking:.status.networking}}' \
  >"$evidence_dir/kubernetes-cilium-endpoint-before.json"
printf '%s\n' "$baseline_agent_endpoint" | jq -S '{id,externalIdentifiers:.status["external-identifiers"],networking:.status.networking.addressing,status:{state:.status.state,identity:.status.identity,policy:.status.policy,health:.status.health}}' \
  >"$evidence_dir/cilium-endpoint-before.json"
printf '%s\n' "$baseline_peer_endpoint" | jq -S '{id,externalIdentifiers:.status["external-identifiers"],networking:.status.networking.addressing,status:{state:.status.state,identity:.status.identity,policy:.status.policy,health:.status.health}}' \
  >"$evidence_dir/cilium-endpoint-peer-before.json"
if ! gitaly_tcp_probe "$gitaly_primary_ip" "$gitaly_grpc_port" "$evidence_dir/gitaly-probe-before.txt" \
  || ! grep -qx 'cilium_probe=connected' "$evidence_dir/gitaly-probe-before.txt"; then
  echo 'refusing injection: the read-only toolbox-to-primary Gitaly TCP probe did not succeed at baseline' >&2
  exit 2
fi
if ! gitaly_tcp_probe "$healthy_peer_ip" "$healthy_peer_grpc_port" "$evidence_dir/gitaly-peer-probe-before.txt" \
  || ! grep -qx 'cilium_probe=connected' "$evidence_dir/gitaly-peer-probe-before.txt"; then
  echo 'refusing injection: the read-only toolbox-to-peer Gitaly TCP positive-control probe did not succeed at baseline' >&2
  exit 2
fi

auth=$(printf '%s:%s' "$GIT_AUTH_USER" "$GIT_AUTH_TOKEN" | base64 | tr -d '\n')
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Basic $auth"
unset auth

git ls-remote --refs "$GIT_REMOTE_URL" >"$evidence_dir/refs-before.txt"
grep -q $'\trefs/heads/main$' "$evidence_dir/refs-before.txt" || {
  echo 'refusing injection: the spike acceptance project has no main branch' >&2
  exit 2
}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
branch="spike/failure-injection-$timestamp-$$"
git clone --quiet --branch main --single-branch "$GIT_REMOTE_URL" "$scratch/repo"
git -C "$scratch/repo" config user.name 'GitLab Spike Failure Proof'
git -C "$scratch/repo" config user.email 'gitlab-spike-failure@invalid.example'
printf 'Praefect primary failure acknowledgement proof %s\n' "$timestamp" \
  >"$scratch/repo/.gitlab-spike-failure-proof-$timestamp.txt"
git -C "$scratch/repo" add ".gitlab-spike-failure-proof-$timestamp.txt"
git -C "$scratch/repo" commit --quiet -m "test: record spike primary failure proof $timestamp [skip ci]"
acknowledged_sha=$(git -C "$scratch/repo" rev-parse HEAD)
printf 'branch=%s\ncommit=%s\n' "$branch" "$acknowledged_sha" >"$evidence_dir/write.txt"

fault_touched=1
guarded_fault_label_change isolated "$original_fault_label" "$evidence_dir/isolate-primary.json" yes
kubectl -n "$namespace" get pods \
  -l "app=gitaly,release=gitlab-spike,$fault_key=isolated" -o json \
  >"$evidence_dir/isolated-pods.json"
jq -e --arg pod "$GITALY_PRIMARY_POD" --arg uid "$primary_uid" \
  '(.items | length) == 1 and .items[0].metadata.name == $pod and .items[0].metadata.uid == $uid' \
  "$evidence_dir/isolated-pods.json" >/dev/null
kubectl -n "$namespace" get pods -o wide >"$evidence_dir/pods-during-fault.txt"
wait_for_cilium_isolation

set +e
push_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
push_started_epoch=$(date -u +%s.%N)
push_deadline_epoch=$(($(date -u +%s) + 180))
push_status=1
attempt=0
while (($(date -u +%s) < push_deadline_epoch)); do
  ((attempt += 1))
  remaining_seconds=$((push_deadline_epoch - $(date -u +%s)))
  attempt_timeout=10
  ((remaining_seconds < attempt_timeout)) && attempt_timeout=$remaining_seconds
  ((attempt_timeout > 0)) || break
  attempt_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  timeout --kill-after=1s "${attempt_timeout}s" \
    git -C "$scratch/repo" push --quiet origin "HEAD:refs/heads/$branch" \
    >"$evidence_dir/push-$attempt.stdout" 2>"$evidence_dir/push-$attempt.stderr"
  push_status=$?
  printf 'attempt=%s started_at_utc=%s finished_at_utc=%s exit_status=%s\n' \
    "$attempt" "$attempt_started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$push_status" \
    >>"$evidence_dir/push-attempts.txt"
  if [[ $push_status == 0 ]]; then
    push_acknowledged_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    push_acknowledged_epoch=$(date -u +%s.%N)
    break
  fi
  (($(date -u +%s) < push_deadline_epoch)) && sleep 2
done
set -e
printf 'push_started_at_utc=%s\npush_exit_status=%s\npush_attempt_count=%s\n' \
  "$push_started" "$push_status" "$attempt" >>"$evidence_dir/write.txt"
if [[ $push_status != 0 ]]; then
  printf 'acknowledged_write=no\n' >>"$evidence_dir/write.txt"
  praefect_metadata >"$evidence_dir/praefect-metadata-after-failed-push.txt" 2>&1 || true
  echo 'push was not acknowledged; the result is a failed fault proof, not a durability result' >&2
  exit 1
fi
push_rto_seconds=$(awk -v started="$push_started_epoch" -v ack="$push_acknowledged_epoch" \
  'BEGIN { printf "%.3f", ack-started }')
printf 'push_acknowledged_at_utc=%s\npush_ack_rto_seconds=%s\n' \
  "$push_acknowledged_at" "$push_rto_seconds" >>"$evidence_dir/write.txt"
printf 'acknowledged_write=yes\n' >>"$evidence_dir/write.txt"
git ls-remote --refs "$GIT_REMOTE_URL" >"$evidence_dir/refs-during-fault.txt"
during_sha=$(awk -v ref="refs/heads/$branch" '$2 == ref {print $1}' "$evidence_dir/refs-during-fault.txt")
if [[ $during_sha != "$acknowledged_sha" ]]; then
  echo 'the acknowledged branch was not readable at its exact commit while the primary was isolated' >&2
  exit 1
fi
git clone --quiet --branch "$branch" --single-branch "$GIT_REMOTE_URL" "$scratch/read-during-fault"
read_during_sha=$(git -C "$scratch/read-during-fault" rev-parse HEAD)
[[ $read_during_sha == "$acknowledged_sha" ]]
git -C "$scratch/read-during-fault" fsck --full --strict \
  >"$evidence_dir/git-fsck-during-fault.txt" 2>&1
printf 'read_during_fault_commit=%s\n' "$read_during_sha" >>"$evidence_dir/write.txt"
wait_for_promotion
promoted_storage=$(cat "$evidence_dir/promoted-storage.txt")
if [[ ! $promoted_storage =~ ^${statefulset}-[0-9]+$ || $promoted_storage == "$GITALY_PRIMARY_POD" ]]; then
  echo "Praefect reported unexpected promoted storage: $promoted_storage" >&2
  exit 1
fi
promoted_pod_json=$(capture_resource pod "$promoted_storage")
printf '%s\n' "$promoted_pod_json" | jq -e --arg name "$promoted_storage" --arg sts "$statefulset" '
  .metadata.namespace == "gitlab-spike" and .metadata.name == $name and
  any(.metadata.ownerReferences[]?; .controller == true and .kind == "StatefulSet" and .name == $sts)
' >/dev/null || {
  echo 'Praefect promotion target is not another pod in the spike Gitaly StatefulSet' >&2
  exit 1
}
printf 'promoted_storage=%s\n' "$promoted_storage" >>"$evidence_dir/write.txt"
restore_fault_label
printf 'fault_label_removed_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$evidence_dir/metadata.txt"
wait_for_current_replicas "$evidence_dir/praefect-metadata-after.txt"
kubectl -n "$namespace" get pods -o wide >"$evidence_dir/pods-after-recovery.txt"
praefect_check >"$evidence_dir/praefect-check-after.txt" 2>&1
assert_all_replicas_current "$evidence_dir/praefect-metadata-after.txt"
replica_checksums >"$evidence_dir/replica-checksums-after.txt" 2>&1
assert_replica_checksums_match "$evidence_dir/replica-checksums-after.txt"
git ls-remote --refs "$GIT_REMOTE_URL" >"$evidence_dir/refs-after-recovery.txt"
validate_refs "$evidence_dir/refs-before.txt" "$evidence_dir/refs-after-recovery.txt"
recovered_sha=$(awk -v ref="refs/heads/$branch" '$2 == ref {print $1}' "$evidence_dir/refs-after-recovery.txt")
if [[ $recovered_sha != "$acknowledged_sha" ]]; then
  echo 'acknowledged commit ref did not survive recovery at its original SHA' >&2
  exit 1
fi
git clone --quiet --branch "$branch" --single-branch "$GIT_REMOTE_URL" "$scratch/read-after-recovery"
recovered_read_sha=$(git -C "$scratch/read-after-recovery" rev-parse HEAD)
[[ $recovered_read_sha == "$acknowledged_sha" ]]
git -C "$scratch/read-after-recovery" fsck --full --strict \
  >"$evidence_dir/git-fsck-after-recovery.txt" 2>&1
printf 'recovered_ref_commit=%s\nread_after_recovery_commit=%s\nref_integrity=passed\nreplica_generation=converged\nchecksum_match=passed\ngit_fsck=passed\n' \
  "$recovered_sha" "$recovered_read_sha" >>"$evidence_dir/write.txt"
printf 'finished_at_utc=%s\nevidence_dir=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$evidence_dir"
