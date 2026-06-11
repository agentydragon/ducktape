#!/usr/bin/env bash
set -euo pipefail

echo "::demo::status-before"
ember-python --status || true

echo "::demo::persist-state"
ember-python -c "x = 41"
ember-python -c "x += 1"
PERSISTED=$(ember-python -c "print(x)")
printf 'persisted_value=%s\n' "$PERSISTED"

echo "::demo::restart"
ember-python --restart
POST_RESTART=$(ember-python -c "print(globals().get('x'))")
printf 'post_restart=%s\n' "$POST_RESTART"

echo "::demo::heredoc"
HEREDOC_OUTPUT=$(
  ember-python <<'PY'
print("strings with 'quotes' and $variables stay literal")
PY
)
printf 'heredoc_output=%s\n' "$HEREDOC_OUTPUT"

WORKSPACE="${EMBER_WORKSPACE_DIR:-/var/lib/ember/workspace}"
mkdir -p "$WORKSPACE"

cat <<'PY' >"$WORKSPACE/helper.py"
def hello() -> str:
    return "Hello World"

def changeable() -> str:
    return "first"
PY

MODULE_VALUE=$(ember-python -c "import helper; print(helper.hello()); print(helper.changeable())")
printf 'module_value=%s\n' "$MODULE_VALUE"

cat <<'PY' >"$WORKSPACE/helper.py"
def hello() -> str:
    return "Hello World"

def changeable() -> str:
    return "second"
PY

RELOADED=$(ember-python -c "import importlib, helper; importlib.reload(helper); print(helper.changeable())")
printf 'module_reloaded=%s\n' "$RELOADED"

echo "::demo::status-after"
ember-python --status || true

ember-python --stop || true
