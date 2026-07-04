#!/usr/bin/env bash
# validate_local.sh — run tools/validate_state.py locally before pushing, so a malformed
# state commit (a YAML typo, a manifest that drifted from the model) is caught without
# waiting on the validate-state CI gate.
#
# The validator imports the backend's own Pydantic models, so it needs:
#   - a python3 with `pydantic>=2` and `pyyaml` importable, and
#   - ui/backend on PYTHONPATH (that's where models.py lives).
# Point VALIDATE_PY at a specific interpreter (a venv, or `uv run` shim) if the default
# `python3` doesn't have those deps. One easy option is:
#   VALIDATE_PY="uv run --with 'pydantic>=2' --with pyyaml python" tools/validate_local.sh
#
# Exit codes: 0 = validated OK; 1 = VALIDATION FAILED; 2 = environment can't run it
# (caller decides — push_state.sh warns and continues, since CI is the independent gate).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PY="${VALIDATE_PY:-python3}"
command -v "${PY%% *}" >/dev/null 2>&1 || {
  echo "validate_local: no '$PY' interpreter" >&2
  exit 2
}

# Confirm the runtime deps are importable; if not, this environment can't validate (exit 2),
# so the caller falls back to relying on the CI gate rather than blocking on a missing dep.
if ! $PY -c 'import pydantic, yaml' >/dev/null 2>&1; then
  echo "validate_local: '$PY' lacks pydantic/pyyaml — set VALIDATE_PY to an interpreter that has them" >&2
  exit 2
fi

if PYTHONPATH="ui/backend" $PY tools/validate_state.py; then
  exit 0
else
  exit 1
fi
