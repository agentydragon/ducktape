#!/usr/bin/env bash
# Rename every py_library target whose name ends in "_py" under the given
# Bazel target pattern, dropping the suffix. Updates same-package short-form
# (":foo_py") and cross-package long-form ("//pkg:foo_py") deps references
# across the entire workspace.
#
# Usage:
#   devinfra/python/rename_py_suffix.sh //augur/...
#   devinfra/python/rename_py_suffix.sh '//cluster/...'
#
# See devinfra/docs/buildozer_rename_py_suffix.md for the convention and the
# rationale for two replace passes.

set -uo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $(basename "$0") <bazel-target-pattern>" >&2
  exit 2
fi
pattern="$1"

if ! command -v buildozer >/dev/null; then
  echo "buildozer not on PATH. Load the Nix devshell (direnv allow + eval) or run under \`nix shell nixpkgs#bazel-buildtools\`." >&2
  exit 1
fi

targets=$(bazelisk query "kind('py_library', $pattern) intersect attr(name, '_py\$', $pattern)" 2>/dev/null || true)
if [[ -z "$targets" ]]; then
  echo "No matching py_library targets ending in _py under $pattern."
  exit 0
fi

while IFS= read -r label; do
  [[ -z "$label" ]] && continue
  pkg="${label%:*}"
  old_name="${label##*:}"
  new_name="${old_name%_py}"
  if [[ "$new_name" == "$old_name" ]]; then
    continue
  fi
  # Skip if the new name collides with an existing target in the same package.
  if bazelisk query "$pkg:$new_name" >/dev/null 2>&1; then
    echo "skipping $label: $pkg:$new_name already exists (use _lib manually)"
    continue
  fi
  echo "renaming $label -> $pkg:$new_name"
  buildozer "set name $new_name" "$label"
  buildozer "replace deps :$old_name :$new_name" "$pkg:*"
  buildozer "replace deps $label $pkg:$new_name" '//...:*'
done <<<"$targets"
