#!/bin/sh
set -eu

/usr/bin/env bash -c "true"
pre-commit --version
for command in bazel bb bbapi bbr buildifier ducktape-precommit kubeconform prettier ruff shfmt; do
  command -v "$command"
done
bazel --version
test -x "$JAVA_HOME/bin/java"
java -version
test -x "$BB_USE_BAZEL_VERSION"
file "$BB_USE_BAZEL_VERSION" | grep -q 'ELF .* executable'
test "$(bazel --version)" = "$("$BB_USE_BAZEL_VERSION" --version)"
# `bb remote` embeds Bazelisk and invokes Bazel locally to canonicalize flags.
# Exercise Bazel with the same arguments first so a failure preserves Bazel's
# own diagnostics rather than BuildBuddy's intentionally compact error.
bazel_help_tmp="$(mktemp -d)"
"$BB_USE_BAZEL_VERSION" \
  --quiet \
  --ignore_all_rc_files \
  --output_base="$bazel_help_tmp/output_base" \
  --max_idle_secs=10 \
  help flags-as-proto >/dev/null
rm -rf "$bazel_help_tmp"
# BuildBuddy's help path then exercises its embedded Bazelisk lookup without
# scheduling a remote action.
bb help remote >/dev/null
bbr --help >/dev/null

# Execute a real Ducktape hook, rather than only checking that its wrapper is
# on PATH. This proves the Nix package carries its Python dependencies and
# generated modules without a workspace-local virtualenv.
hook_repo="$(mktemp -d)"
trap 'rm -rf "$hook_repo"' EXIT
git init -q "$hook_repo"
git -C "$hook_repo" config user.name smoke
git -C "$hook_repo" config user.email smoke@example.com
printf 'openclaw image hook smoke\n' >"$hook_repo/README.md"
git -C "$hook_repo" add README.md
(cd "$hook_repo" && ducktape-precommit)

config="${TMPDIR:-/tmp}/openclaw-plugin-smoke.json"
plugins="${TMPDIR:-/tmp}/openclaw-plugins.json"
cat >"$config" <<'JSON'
{
  "plugins": {
    "entries": {
      "matrix": {
        "enabled": true
      },
      "brave": {
        "enabled": true
      }
    }
  }
}
JSON

OPENCLAW_CONFIG_PATH="$config" openclaw plugins list --json >"$plugins"
jq '{matrix: [.plugins[] | select(.id == "matrix")], brave: [.plugins[] | select(.id == "brave")], diagnostics}' "$plugins"

bundled_plugin_root() {
  plugin_id=$1
  jq -e --arg id "$plugin_id" '.plugins[] | select(.id == $id and .origin == "bundled" and .status == "loaded")' "$plugins" >/dev/null

  source=$(jq -er --arg id "$plugin_id" '.plugins[] | select(.id == $id) | .source' "$plugins")
  case "$source" in
    */dist/extensions/"$plugin_id"/dist/index.js)
      gateway_root=${source%/dist/extensions/"$plugin_id"/dist/index.js}
      ;;
    */dist-runtime/extensions/"$plugin_id"/dist/index.js)
      gateway_root=${source%/dist-runtime/extensions/"$plugin_id"/dist/index.js}
      ;;
    *)
      echo "unexpected $plugin_id plugin source: $source" >&2
      exit 1
      ;;
  esac
  plugin_root=${source%/dist/index.js}

  test -f "$plugin_root/openclaw.plugin.json"
  test "$(readlink -f "$plugin_root/node_modules/openclaw")" = "$gateway_root"
  test -f "$gateway_root/dist/extensions/$plugin_id/openclaw.plugin.json"
  test -f "$gateway_root/dist-runtime/extensions/$plugin_id/openclaw.plugin.json"
  test "$(readlink -f "$gateway_root/dist-runtime/extensions/$plugin_id/node_modules/openclaw")" = "$gateway_root"
  printf '%s\n' "$plugin_root"
}

matrix_root=$(bundled_plugin_root matrix)
test -d "$matrix_root/node_modules/matrix-js-sdk"
bundled_plugin_root brave >/dev/null
jq -e '[.diagnostics[] | select(.message | contains("blocked plugin candidate"))] | length == 0' "$plugins"
test ! -e /opt/openclaw/plugins/matrix
test ! -e /opt/openclaw/plugins/brave
