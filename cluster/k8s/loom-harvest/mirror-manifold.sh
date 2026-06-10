#!/bin/sh
# Mirror the 2024-07-06 Manifold Markets full-site dump (contracts, comments,
# bets) from Firebase into s3://loom-gym/harvest/raw/manifold-20240706/.
#
# Idempotent: objects already in the bucket (HEAD 200) are skipped, so reruns
# after the initial mirror are cheap verifications. Downloads are checked
# against the pinned sha256 manifest before upload — if Firebase ever serves
# different bytes, the Job fails instead of silently replacing the snapshot.
set -eu

S3_PREFIX="${S3_ENDPOINT}/loom-gym/harvest/raw/manifold-20240706"
FIREBASE_BASE="https://firebasestorage.googleapis.com/v0/b/mantic-markets.appspot.com/o/trade-dumps%2F"

s3curl() {
  curl --fail --silent --show-error --aws-sigv4 "aws:amz:us-east-1:s3" \
    --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" "$@"
}

# A non-HTTP failure (DNS, refused connection) also reports "absent" here; the
# subsequent upload then fails loudly, so real S3 outages still fail the Job.
object_exists() {
  s3curl --head --output /dev/null "${S3_PREFIX}/$1" 2>/dev/null
}

put_if_absent() {
  if object_exists "$2"; then
    echo "=== $2: already mirrored, skipping"
  else
    echo "=== $2: uploading"
    s3curl --upload-file "$1" "${S3_PREFIX}/$2"
  fi
}

# Dump files are ~1.2 GB combined; handle one at a time and delete after
# upload so peak /work usage stays around the largest single file (~1 GB).
while read -r sha256 name; do
  if object_exists "${name}"; then
    echo "=== ${name}: already mirrored, skipping"
    continue
  fi
  echo "=== ${name}: downloading from Firebase"
  curl --fail --silent --show-error --output "/work/${name}" "${FIREBASE_BASE}${name}?alt=media"
  echo "${sha256}  /work/${name}" | sha256sum -c -
  echo "=== ${name}: uploading"
  s3curl --upload-file "/work/${name}" "${S3_PREFIX}/${name}"
  rm "/work/${name}"
done </config/manifold-20240706.sha256

put_if_absent /config/manifold-20240706-readme.md README.md
put_if_absent /config/manifold-20240706.sha256 checksums.sha256
echo "=== mirror complete"
