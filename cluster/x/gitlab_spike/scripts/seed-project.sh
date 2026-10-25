#!/usr/bin/env bash
set -euo pipefail

if [[ ${ALLOW_SPIKE_PROJECT_WRITE:-} != YES || ${BENCH_TARGET:-} != spike/acceptance ]]; then
  echo 'refusing write: set ALLOW_SPIKE_PROJECT_WRITE=YES and BENCH_TARGET=spike/acceptance' >&2
  exit 2
fi
case ${GIT_REMOTE_URL:-} in
  'https://gitlab-spike.allegedly.works/spike/acceptance.git' | \
    'http://gitlab-spike-webservice-default.gitlab-spike.svc:8181/spike/acceptance.git') ;;
  *)
    echo 'GIT_REMOTE_URL must be the exact spike/acceptance clone URL without embedded credentials' >&2
    exit 2
    ;;
esac
if [[ -z ${GIT_AUTH_USER:-} || -z ${GIT_AUTH_TOKEN:-} ]]; then
  echo 'GIT_AUTH_USER and GIT_AUTH_TOKEN must come from the approved secret handoff' >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
harness_dir=$(cd -- "$script_dir/.." && pwd)
repo_root=$(cd -- "$script_dir/../../../.." && pwd)
seed_dir=${1:-$repo_root/cluster/generated/gitlab-spike/demo-seed}
for file in demo-seed.k8s.yaml kustomization.yaml; do
  if [[ ! -f $seed_dir/$file ]]; then
    echo "generated demo seed is missing $file: $seed_dir" >&2
    exit 2
  fi
done
if [[ ! -f $harness_dir/seed/k8s/image-pins/kustomization.yaml ]]; then
  echo "native image-pins component is missing from $harness_dir/seed/k8s/image-pins" >&2
  exit 2
fi

umask 077
scratch=$(mktemp -d /tmp/gitlab-spike-seed.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
auth=$(printf '%s:%s' "$GIT_AUTH_USER" "$GIT_AUTH_TOKEN" | base64 | tr -d '\n')
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Basic $auth"
unset auth

git clone --quiet "$GIT_REMOTE_URL" "$scratch/project"
git -C "$scratch/project" config user.name 'GitLab Spike Harness'
git -C "$scratch/project" config user.email 'gitlab-spike-harness@invalid.example'

mkdir -p "$scratch/project/k8s"
cp "$seed_dir/demo-seed.k8s.yaml" "$seed_dir/kustomization.yaml" "$scratch/project/k8s/"
mkdir -p "$scratch/project/k8s/image-pins"
cp "$harness_dir/seed/k8s/image-pins/kustomization.yaml" "$scratch/project/k8s/image-pins/"
cp "$harness_dir/.gitlab-ci.yml" "$harness_dir/go.mod" "$harness_dir/main.go" "$harness_dir/main_test.go" "$scratch/project/"

git -C "$scratch/project" add -- .gitlab-ci.yml go.mod main.go main_test.go k8s
if git -C "$scratch/project" diff --cached --quiet; then
  echo 'spike acceptance project already has the current harness seed'
  exit 0
fi
git -C "$scratch/project" commit --quiet -m 'chore: seed GitLab spike acceptance project'
git -C "$scratch/project" push origin HEAD:main
printf 'seeded project=%s commit=%s\n' 'spike/acceptance' "$(git -C "$scratch/project" rev-parse HEAD)"
