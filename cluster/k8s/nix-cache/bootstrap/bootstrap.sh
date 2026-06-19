#!/usr/bin/bash
# Idempotently ensure required Attic caches exist.
#
# Mints an ephemeral admin JWT (5-minute validity) by `kubectl exec`-ing
# into the running attic pod and invoking atticadm there. The HS256
# signing secret never leaves the attic pod / nix-cache namespace; this
# Job just consumes the resulting JWT to call attic's REST API.
#
# Idempotent: GET /_api/v1/cache-config/<name> first; only POST to create
# when the cache is missing.
#
# TODO: cleaner approach — use the `attic` (client) CLI for cache CRUD
# instead of hand-rolled curl + JSON body. The upstream
# `ghcr.io/zhaofengli/attic` image only ships `atticadm` and `atticd`, so
# pulling in the client requires either a Bazel rules_rust build of attic
# upstream, a multi-stage Dockerfile that nix-installs `attic-client`, or
# a separate image. This script's curl approach assumes attic's
# `/_api/v1/cache-config/:cache` shape is stable; if upstream ever bumps
# it, swap to the client.
#
# Public keys are NOT extracted by this script — fetch them from the
# unauthenticated endpoint after the cache exists:
#   curl https://cache.allegedly.works/<cache>/nix-cache-info
# Paste each resulting `Trusted-Public-Key:` value into nix/attic-pubkeys.json
# (the single source of truth, read by both nix/nixos/modules/attic-substituter.nix
# and the nix-attic-push CI workflow).
#
# TODO: nice-to-have — auto-fetch the pubkey post-creation and push it
# back into nix/attic-pubkeys.json via the github-secrets-sync-pat PAT
# (same mechanism the rotator uses for SOPS files). Today we just paste it
# once per cluster lifetime; the pubkey only changes on full cluster
# rebuild, so the manual step is rare.
#
# To re-run after editing this script, the
# `kustomize.toolkit.fluxcd.io/force: enabled` annotation on the Job tells
# Flux to delete-and-recreate it on next reconcile.

set -euo pipefail

ATTIC_NAMESPACE="${ATTIC_NAMESPACE:-nix-cache}"
ATTIC_DEPLOYMENT="${ATTIC_DEPLOYMENT:-deploy/attic}"
SERVER="${ATTIC_SERVER_URL:-http://attic.nix-cache.svc.cluster.local:8080}"
# Space-separated list of caches to ensure exist.
# - main: ducktape's general-purpose cache (CI pushes ducktape flake outputs).
# - gaffer: gaffer-private's CI pushes drivefs/drivectl closures.
# Both private (is_public: false in the POST body); reader/writer JWTs are
# minted by the attic-jwt-rotation CronJob.
CACHES="${CACHES:-main gaffer}"

echo "[bootstrap] minting 5-minute admin JWT via kubectl exec ${ATTIC_DEPLOYMENT}..."
# atticadm tries to read a default config from a path it doesn't have
# permission to (EACCES) when -f isn't passed; the running attic pod has
# its own server.toml at /config, so reuse that.
ADMIN_JWT=$(kubectl -n "$ATTIC_NAMESPACE" exec "$ATTIC_DEPLOYMENT" -- \
  atticadm -f /config/server.toml make-token \
  --sub bootstrap-admin \
  --validity '5 minutes' \
  --pull '*' --push '*' --create-cache '*' | tr -d '[:space:]')

if [ -z "$ADMIN_JWT" ]; then
  echo "ERROR: atticadm make-token returned empty output" >&2
  exit 1
fi

# Default body matches what `attic cache create <name>` sends with no flags
# (cf. attic upstream client/src/command/cache.rs::create_cache):
#   keypair = Generate, is_public = false, store_dir = /nix/store,
#   priority = 41, upstream_cache_key_names = [].
CREATE_BODY='{"keypair":"Generate","is_public":false,"store_dir":"/nix/store","priority":41,"upstream_cache_key_names":[]}'

for CACHE in $CACHES; do
  printf '[bootstrap] cache %s: ' "$CACHE"
  STATUS=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $ADMIN_JWT" \
    "${SERVER}/_api/v1/cache-config/${CACHE}")
  if [ "$STATUS" = "200" ]; then
    echo "exists (GET 200)"
  else
    echo "missing (GET $STATUS) — creating"
    CREATE_STATUS=$(curl -sS -o /tmp/create.out -w '%{http_code}' \
      -X POST -H "Authorization: Bearer $ADMIN_JWT" \
      -H 'Content-Type: application/json' \
      -d "$CREATE_BODY" \
      "${SERVER}/_api/v1/cache-config/${CACHE}")
    if [ "$CREATE_STATUS" -ge 200 ] && [ "$CREATE_STATUS" -lt 300 ]; then
      echo "[bootstrap] cache $CACHE: created (POST $CREATE_STATUS)"
    else
      echo "ERROR: cache $CACHE: POST returned $CREATE_STATUS" >&2
      cat /tmp/create.out >&2
      echo >&2
      exit 1
    fi
  fi
  # Print the public key for visibility (nix-cache-info is unauthenticated).
  printf '[bootstrap] cache %s: nix-cache-info:\n' "$CACHE"
  curl -sSf "${SERVER}/${CACHE}/nix-cache-info" || true
done

echo "[bootstrap] done."
