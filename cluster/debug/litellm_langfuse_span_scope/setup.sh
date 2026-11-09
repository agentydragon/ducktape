#!/usr/bin/env bash
# One-time setup of the scratch namespace: dependencies, generated credentials, copies of
# the production cheap-experiments key and image pull secret, and Langfuse.
set -euo pipefail
cd "$(dirname "$0")"
NS=scratch-langfuse-tracing

kubectl apply -f deps.yaml

rand() { openssl rand -hex "$1"; }
# Generated once: the databases keep the passwords they first started with.
kubectl -n "$NS" get secret scratch-secrets >/dev/null 2>&1 || kubectl -n "$NS" create secret generic scratch-secrets \
  --from-literal=postgres-password="$(rand 16)" \
  --from-literal=valkey-password="$(rand 16)" \
  --from-literal=clickhouse-password="$(rand 16)" \
  --from-literal=s3-access-key-id="scratch$(rand 4)" \
  --from-literal=s3-secret-access-key="$(rand 16)" \
  --from-literal=salt="$(rand 16)" \
  --from-literal=encryption-key="$(rand 32)" \
  --from-literal=nextauth-secret="$(rand 16)" \
  --from-literal=langfuse-public-key="pk-lf-scratch-$(rand 8)" \
  --from-literal=langfuse-secret-key="sk-lf-scratch-$(rand 16)" \
  --from-literal=langfuse-user-password="$(rand 16)" \
  --from-literal=litellm-master-key="sk-scratch-$(rand 16)"

# Copy a production Secret into the scratch namespace without printing its value.
copy_secret() {
  kubectl -n "$1" get secret "$2" -o json \
    | jq --arg ns "$NS" --arg name "$3" '{apiVersion, kind, type, data, metadata: {name: $name, namespace: $ns}}' \
    | kubectl apply -f -
}
copy_secret litellm litellm-key-cheap-experiments upstream-key
copy_secret litellm forgejo-images-creds forgejo-images-creds

kubectl -n "$NS" rollout status deploy/postgres deploy/valkey deploy/clickhouse deploy/s3 --timeout=5m
until kubectl -n "$NS" exec deploy/s3 -- sh -c 'echo "s3.bucket.create -name langfuse" | weed shell -master=localhost:9333' 2>&1 | grep -q -v -i error; do sleep 5; done
helm upgrade --install langfuse langfuse --repo https://langfuse.github.io/langfuse-k8s --version 2.1.6 \
  -n "$NS" -f langfuse-values.yaml --wait --timeout 15m
