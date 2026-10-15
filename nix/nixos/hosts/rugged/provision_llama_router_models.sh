#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 3 ]]; then
  echo "usage: provision_llama_router_models.sh MANIFEST MODEL_STORE OLLAMA_BLOBS_DIR" >&2
  exit 2
fi

manifest=$1
model_store=$2
ollama_blobs_dir=$3
blob_dir="$model_store/blobs"
staging_dir="$model_store/.staging"

if [[ ! -r $manifest ]]; then
  echo "pinned model manifest is not readable: $manifest" >&2
  exit 1
fi

if ! jq --exit-status '
  . as $manifest
  | $manifest.version == 1
  and ($manifest.artifacts | type == "object" and length > 0)
  and all($manifest.artifacts | to_entries[];
    (.key | test("^[a-z0-9-]+$"))
    and (.value.sha256 | test("^[0-9a-f]{64}$"))
    and (.value.size_bytes | type == "number" and . > 0 and floor == .)
    and (.value.url | test("^https?://[^[:space:]]+$"))
  )
' "$manifest" >/dev/null; then
  echo "pinned model manifest has an invalid version or artifact: $manifest" >&2
  exit 1
fi

verify_artifact() {
  local file=$1 expected_digest=$2 expected_size=$3 actual_digest actual_size
  [[ -f $file && ! -L $file ]] || return 1
  actual_size=$(stat --format=%s -- "$file")
  [[ $actual_size == "$expected_size" ]] || return 1
  actual_digest=$(sha256sum -- "$file")
  actual_digest=${actual_digest%% *}
  [[ $actual_digest == "$expected_digest" ]]
}

install -d -- "$blob_dir" "$staging_dir"

while IFS=$'\t' read -r artifact_id digest expected_size url; do
  if [[ ! $artifact_id =~ ^[a-z0-9-]+$ || ! $digest =~ ^[0-9a-f]{64}$ || ! $expected_size =~ ^[1-9][0-9]*$ ]]; then
    echo "invalid artifact row in pinned manifest: $artifact_id" >&2
    exit 1
  fi

  destination="$blob_dir/sha256-$digest"
  if [[ -e $destination || -L $destination ]]; then
    if ! verify_artifact "$destination" "$digest" "$expected_size"; then
      echo "content-addressed model cache entry failed verification; preserving it: $destination" >&2
      exit 1
    fi
    echo "verified cached artifact $artifact_id ($digest)"
    continue
  fi

  ollama_blob="$ollama_blobs_dir/sha256-$digest"
  import_tmp="$blob_dir/.import-$digest-$$"
  if [[ -f $ollama_blob ]]; then
    if verify_artifact "$ollama_blob" "$digest" "$expected_size"; then
      if ! ln -- "$ollama_blob" "$import_tmp" 2>/dev/null; then
        cp --reflink=auto --sparse=always -- "$ollama_blob" "$import_tmp"
        if ! verify_artifact "$import_tmp" "$digest" "$expected_size"; then
          rm -f -- "$import_tmp"
          echo "Ollama cache copy failed verification for $artifact_id: $ollama_blob" >&2
          exit 1
        fi
      fi
      if [[ -e $destination || -L $destination ]]; then
        rm -f -- "$import_tmp"
        if ! verify_artifact "$destination" "$digest" "$expected_size"; then
          echo "a concurrent writer created an invalid cache entry: $destination" >&2
          exit 1
        fi
      else
        mv --no-target-directory -- "$import_tmp" "$destination"
      fi
      echo "imported verified Ollama artifact $artifact_id ($digest)"
      continue
    fi
    echo "Ollama blob failed digest/size verification; leaving it untouched and fetching pinned artifact $artifact_id" >&2
  fi

  partial="$staging_dir/$digest.partial"
  if [[ -f $partial ]]; then
    partial_size=$(stat --format=%s -- "$partial")
    if [[ $partial_size == "$expected_size" ]]; then
      if verify_artifact "$partial" "$digest" "$expected_size"; then
        mv --no-target-directory -- "$partial" "$destination"
        rm -f -- "$partial.aria2"
        echo "promoted completed verified partial artifact $artifact_id ($digest)"
        continue
      fi
      rm -f -- "$partial" "$partial.aria2"
    elif [[ $partial_size -gt $expected_size ]]; then
      rm -f -- "$partial" "$partial.aria2"
    fi
  fi

  aria2_args=(
    --allow-overwrite=true
    --auto-file-renaming=false
    --checksum="sha-256=$digest"
    --check-integrity=true
    --continue=true
    --connect-timeout=10
    --timeout=60
    --max-tries=3
    --max-file-not-found=2
    --retry-wait=2
    --file-allocation=none
    --console-log-level=warn
    --dir="$staging_dir"
    --out="$digest.partial"
  )
  if [[ -n ${SSL_CERT_FILE:-} ]]; then
    aria2_args+=("--ca-certificate=$SSL_CERT_FILE")
  fi
  aria2_args+=("$url")
  aria2c "${aria2_args[@]}"
  if ! verify_artifact "$partial" "$digest" "$expected_size"; then
    rm -f -- "$partial" "$partial.aria2"
    echo "downloaded artifact failed pinned digest/size verification: $artifact_id ($url)" >&2
    exit 1
  fi

  if [[ -e $destination || -L $destination ]]; then
    rm -f -- "$partial" "$partial.aria2"
    if ! verify_artifact "$destination" "$digest" "$expected_size"; then
      echo "a concurrent writer created an invalid cache entry: $destination" >&2
      exit 1
    fi
  else
    mv --no-target-directory -- "$partial" "$destination"
  fi
  echo "downloaded and verified pinned artifact $artifact_id ($digest)"
done < <(jq --raw-output '
  .artifacts | to_entries[] | [.key, .value.sha256, (.value.size_bytes | tostring), .value.url] | @tsv
' "$manifest")
