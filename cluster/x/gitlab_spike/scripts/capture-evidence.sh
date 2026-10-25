#!/usr/bin/env bash
set -uo pipefail

out=${1:?usage: capture-evidence.sh /absolute/path/to/evidence-dir}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../../../.." && pwd)
case "$out" in
  /*) ;;
  *)
    echo 'evidence directory must be an absolute path outside the repository' >&2
    exit 2
    ;;
esac
case "$out/" in
  "$repo_root/"*)
    echo 'evidence directory cannot be inside the repository' >&2
    exit 2
    ;;
esac
umask 077
mkdir -p "$out"

record() {
  local file=$1
  shift
  {
    printf 'command:'
    printf ' %q' "$@"
    printf '\n'
    if "$@"; then
      :
    else
      local status=$?
      printf 'command_exit_status=%s\n' "$status"
    fi
  } >"$out/$file" 2>&1
}

filter_json() {
  local file=$1
  local resource=$2
  local selector=${3:-}
  {
    if [ -n "$selector" ]; then
      kubectl get "$resource" -A -l "$selector" -o json
    else
      kubectl get "$resource" -A -o json
    fi
  } 2>"$out/$file.stderr" | jq -S '
    [.items[] | {
      namespace: .metadata.namespace,
      name: .metadata.name,
      generation: .metadata.generation,
      observedGeneration: .status.observedGeneration,
      spec: {
        path: .spec.path,
        ref: .spec.ref,
        sourceRef: .spec.sourceRef,
        image: .spec.image,
        imageRepositoryRef: .spec.imageRepositoryRef,
        policy: .spec.policy,
        update: .spec.update,
        type: .spec.type
      },
      conditions: [(.status.conditions // [])[] | {
        type: .type,
        status: .status,
        reason: .reason,
        lastTransitionTime: .lastTransitionTime
      }]
    }]
  ' >"$out/$file.json" 2>>"$out/$file.stderr" || true
}

{
  printf 'captured_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_commit=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unavailable)"
  printf 'cluster_context=%s\n' "$(kubectl config current-context 2>/dev/null || echo unavailable)"
  printf 'kubectl_client=%s\n' "$(kubectl version --client --output=yaml 2>/dev/null | sed -n '1,8p' || echo unavailable)"
} >"$out/run-metadata.txt"

for namespace in gitlab-spike gitlab-spike-ci gitlab-spike-demo; do
  record "${namespace}-pods.txt" kubectl -n "$namespace" get pods -o wide
  record "${namespace}-workloads.txt" kubectl -n "$namespace" get deployments,statefulsets,services,persistentvolumeclaims
  record "${namespace}-events.txt" kubectl -n "$namespace" get events --sort-by=.metadata.creationTimestamp \
    -o 'custom-columns=LAST:.lastTimestamp,TYPE:.type,REASON:.reason,OBJECT:.involvedObject.name' --no-headers
done

{
  kubectl -n flux-system get terraform.infra.contrib.fluxcd.io gitlab-spike -o json 2>/dev/null \
    | jq -S '{metadata:{name:.metadata.name,namespace:.metadata.namespace,uid:.metadata.uid,generation:.metadata.generation,resourceVersion:.metadata.resourceVersion},status:{observedGeneration:.status.observedGeneration,conditions:[(.status.conditions // [])[]|{type,status,reason,lastTransitionTime}]}}'
} >"$out/terraform-status.json" 2>"$out/terraform-status.stderr" || true

filter_json flux-kustomizations kustomizations.kustomize.toolkit.fluxcd.io
filter_json flux-gitrepositories gitrepositories.source.toolkit.fluxcd.io
filter_json flux-image-repositories imagerepositories.image.toolkit.fluxcd.io
filter_json flux-image-policies imagepolicies.image.toolkit.fluxcd.io
filter_json flux-image-automations imageupdateautomations.image.toolkit.fluxcd.io
filter_json flux-receivers receivers.notification.toolkit.fluxcd.io

{
  for namespace in gitlab-spike gitlab-spike-ci gitlab-spike-demo; do
    kubectl -n "$namespace" get pods -o json 2>/dev/null \
      | jq -r '.items[] | . as $pod | ($pod.status.containerStatuses // [])[]? | [
        $pod.metadata.namespace,
        $pod.metadata.name,
        .name,
        .image,
        .imageID,
        (.ready|tostring)
      ] | @tsv'
  done
} >"$out/container-image-ids.tsv" 2>"$out/container-image-ids.stderr" || true

{
  printf 'endpoint,status,time_seconds\n'
  for pair in \
    'gitlab,https://gitlab-spike.allegedly.works/users/sign_in' \
    'registry,https://registry-gitlab-spike.allegedly.works/v2/'; do
    label=${pair%%,*}
    endpoint=${pair#*,}
    result=$(curl --silent --show-error --output /dev/null --write-out '%{http_code},%{time_total}' \
      --max-time 15 "$endpoint" 2>/dev/null) || result='request_failed'
    printf '%s,%s\n' "$label" "$result"
  done
} >"$out/anonymous-endpoints.csv"

printf 'evidence_dir=%s\n' "$out"
