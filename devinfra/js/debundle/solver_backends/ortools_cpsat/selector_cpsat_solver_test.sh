#!/usr/bin/env bash
# The sidecar is a CP-SAT build with assertions off: CP-SAT warns "running in
# debug mode" on every solve of a binary compiled without NDEBUG.
set -euo pipefail

protoc="$1"
proto="$2"
solver="$3"
package=ducktape.debundle.solver_backends.ortools_cpsat

"$protoc" --encode="$package.SelectorCpSatRequest" "$proto" >request.pb <<'EOF'
variables { dense_domain { value_count: 1 } }
max_alternatives_per_variable: 5
EOF
"$solver" <request.pb >response.pb 2>stderr.txt

# Anti-vacuity: the request reached CP-SAT and was solved.
response="$("$protoc" --decode="$package.SelectorCpSatResponse" "$proto" <response.pb)"
grep -q SOLVER_STATUS_SATISFIABLE <<<"$response"

if grep -q "debug mode" stderr.txt; then
  echo "selector_cpsat_solver was built without NDEBUG:" >&2
  cat stderr.txt >&2
  exit 1
fi
