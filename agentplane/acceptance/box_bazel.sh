#!/usr/bin/env bash
# Bazel inside a claude-ai runner box (an agentplane sandbox): `box_bazel.sh <command> [args...]`.
# Deviation from the RBE-only rule (<../../devinfra/docs/rbe_workflows.md>): the box's egress
# refuses BuildBuddy, so Bazel runs locally on the workspace bazelrc minus its RBE import.
# Everything it writes lives under /state, which survives between the box's commands.
set -euo pipefail

src=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
state=/state
version=$(<"$src/.bazelversion")
bazel=$state/bazel-$version
rbe_import='import %workspace%/devinfra/bazel/rbe.bazelrc'

if [[ ! -x $bazel ]]; then
  curl -sSfLo "$bazel.part" "https://releases.bazel.build/$version/release/bazel-$version-linux-x86_64"
  chmod +x "$bazel.part"
  mv "$bazel.part" "$bazel"
fi

# Bazel's JVM downloads through the egress proxy, which re-signs TLS with the CA in SSL_CERT_FILE.
openssl pkcs12 -export -nokeys -jdktrust anyExtendedKeyUsage -in "$SSL_CERT_FILE" \
  -out "$state/proxy-ca.p12" -passout pass:changeit

# kubectl reaches the API server through the egress proxy, which swaps this placeholder for the
# box's own workload token.
cat >"$state/kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: in-cluster
    cluster:
      server: https://kubernetes.default.svc.cluster.local
      certificate-authority: $SSL_CERT_FILE
users:
  - name: workload
    user:
      token: agentplane-credential-kubernetes-workload
contexts:
  - name: in-cluster
    context:
      cluster: in-cluster
      user: workload
current-context: in-cluster
EOF

grep -qx "$rbe_import" "$src/.bazelrc" || {
  echo "box_bazel.sh: $src/.bazelrc no longer has the line: $rbe_import" >&2
  exit 1
}
{
  grep -vx "$rbe_import" "$src/.bazelrc"
  # Lint is CI's job; a box needs the targets themselves.
  echo 'build --config=nolint'
  echo 'common --config=ai_agent'
  # Bazel scrubs the test environment; clients in a test reach anything only through the proxy.
  echo 'test --test_env=HTTPS_PROXY --test_env=HTTP_PROXY --test_env=NO_PROXY --test_env=https_proxy --test_env=http_proxy --test_env=no_proxy --test_env=SSL_CERT_FILE'
} >"$state/box.bazelrc"

export KUBECONFIG=$state/kubeconfig
cd "$src"
exec "$bazel" --output_user_root="$state/bazel-root" --noworkspace_rc --bazelrc="$state/box.bazelrc" \
  --host_jvm_args=-Djavax.net.ssl.trustStore="$state/proxy-ca.p12" \
  --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit \
  --host_jvm_args=-Djavax.net.ssl.trustStoreType=PKCS12 \
  "$@"
