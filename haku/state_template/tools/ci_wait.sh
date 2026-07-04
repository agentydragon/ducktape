#!/usr/bin/env bash
# ci_wait.sh — block until every Forgejo Actions run for the current local HEAD concludes.
# End-of-run ritual: a run is not "done" until the CI its push triggered is verified —
# green means done, red means fix before finishing.
#
# Auth is read from the environment (no operator specifics baked in). Fill these for your
# instance, using the same repo-write user the deployment uses:
#   FORGEJO_API_BASE — the repo's Forgejo API base, e.g.
#                      https://your-forgejo.example.com/api/v1/repos/<owner>/haku-state
#   FORGEJO_USER     — a user that can read the repo's actions (typically <owner>)
#   FORGEJO_TOKEN    — that user's token/password
# If the creds live in a k8s Secret, fetch them once before running, e.g.:
#   export FORGEJO_USER=<owner>
#   export FORGEJO_TOKEN="$(kubectl -n <agent-namespace> get secret <git-write-secret> \
#     -o jsonpath='{.data.password}' | base64 -d)"
#
# Exit codes: 0 = all runs for HEAD succeeded; 1 = at least one FAILED (react before
# completing the run); 2 = couldn't verify (API error / no runs appeared — investigate,
# don't silently complete).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
SHA="$(git rev-parse HEAD)"
: "${FORGEJO_API_BASE:?set FORGEJO_API_BASE to the repo Forgejo API base URL}"
: "${FORGEJO_USER:?set FORGEJO_USER}"
: "${FORGEJO_TOKEN:?set FORGEJO_TOKEN}"
API="${FORGEJO_API_BASE%/}/actions/tasks?limit=20"
DEADLINE=$((SECONDS + ${CI_WAIT_TIMEOUT:-900}))
APPEAR_DEADLINE=$((SECONDS + 120)) # validate-state triggers on every main push, so ≥1 run must appear

while :; do
  json="$(curl -sf "$API" -u "$FORGEJO_USER:$FORGEJO_TOKEN")" || {
    echo "ci_wait: Forgejo API error" >&2
    exit 2
  }
  read -r total pending failed <<<"$(printf '%s' "$json" | python3 -c "
import json, sys
sha = '$SHA'
runs = [r for r in json.load(sys.stdin)['workflow_runs'] if r['head_sha'] == sha]
pending = sum(r['status'] in ('running', 'waiting') for r in runs)
failed = sum(r['status'] not in ('running', 'waiting', 'success') for r in runs)
print(len(runs), pending, failed)
for r in runs:
    print(f\"  {r['name']}: {r['status']}\", file=sys.stderr)
")"
  if [ "$total" -eq 0 ]; then
    [ "$SECONDS" -lt "$APPEAR_DEADLINE" ] && {
      sleep 10
      continue
    }
    echo "ci_wait: no CI runs appeared for HEAD ${SHA:0:7} — runner down or push not registered?" >&2
    exit 2
  fi
  if [ "$pending" -gt 0 ]; then
    [ "$SECONDS" -lt "$DEADLINE" ] && {
      sleep 15
      continue
    }
    echo "ci_wait: timed out with $pending run(s) still pending for ${SHA:0:7}" >&2
    exit 2
  fi
  if [ "$failed" -gt 0 ]; then
    echo "ci_wait: $failed run(s) FAILED for ${SHA:0:7} — fix before completing the run" >&2
    exit 1
  fi
  echo "ci_wait: all $total run(s) green for ${SHA:0:7}"
  exit 0
done
