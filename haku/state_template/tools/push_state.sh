#!/usr/bin/env bash
# push_state.sh — validate, then push haku-state to origin/main. Run from anywhere in the
# repo. Optionally waits on CI afterward (see CI_WAIT below).
#
# The direct `git push` is tried every run. If the push stalls on an egress throttle
# (the intermittent "curl 28 / sideband disconnect" signature some networks show), it
# falls back to an in-cluster pod that clones the repo and pushes a bundle from inside the
# cluster. The fallback only triggers on that stall signature, so a genuine push error
# still surfaces. Set AGENT_NAMESPACE to your agent's k8s namespace for the fallback to
# work; if you never hit the stall you can ignore it.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
NS="${AGENT_NAMESPACE:-<agent-namespace>}"
# Name of the k8s Secret holding git-write creds (username/password/repo_url keys).
GIT_WRITE_SECRET="${GIT_WRITE_SECRET:-<git-write-secret>}"

# Pre-push gate: never land malformed state on main (the backend reads HEAD live).
# Validation failure (exit 1) BLOCKS the push — fix the data/model first. An environment
# that can't run the validator (exit 2) warns and continues: blocking state persistence on
# a missing interpreter would be worse, and the validate-state CI workflow is the
# independent backstop either way.
rc=0
tools/validate_local.sh || rc=$?
if [ "$rc" -eq 1 ]; then
  echo "push_state: BLOCKED — state validation failed (see above). Fix before pushing." >&2
  exit 1
elif [ "$rc" -eq 2 ]; then
  echo "push_state: WARNING — couldn't run local validation; relying on the CI gate." >&2
fi

# The UI backend + Flux image automation are concurrent writers to main, so rebase onto
# their commits before pushing. A conflict is rare and bails loud.
if ! git pull --rebase origin main >/dev/null 2>/tmp/rebase.err; then
  echo "push_state: 'git pull --rebase origin main' failed (conflict?) — resolve manually:" >&2
  cat /tmp/rebase.err >&2
  exit 1
fi

ERR="$(mktemp)"
if git push -u origin main 2>"$ERR"; then
  echo "push_state: direct OK"
  rm -f "$ERR"
  [ "${CI_WAIT:-0}" = "1" ] && exec tools/ci_wait.sh
  exit 0
fi
if ! grep -qiE 'too slow|sideband|unexpected disconnect|timed out|operation too slow|less than' "$ERR"; then
  echo "push_state: direct push failed (not the egress stall):" >&2
  cat "$ERR" >&2
  rm -f "$ERR"
  exit 1
fi
echo "push_state: direct push stalled on egress throttle — falling back to in-cluster pod"
rm -f "$ERR"

SUF="$$-${RANDOM}"
POD="haku-push-${SUF}"
CM="haku-pushbundle-${SUF}"
cleanup() { kubectl -n "$NS" delete "pod/$POD" "configmap/$CM" --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

B="$(mktemp)"
git bundle create "$B" origin/main..main
kubectl -n "$NS" create configmap "$CM" --from-file=bundle="$B" >/dev/null
rm -f "$B"

# Quoted heredoc keeps $GIT_USER/$GIT_PASS literal for in-pod evaluation; sed fills the
# pod/configmap names (which contain only digits/dashes, safe for sed).
sed -e "s/HAKU_POD/${POD}/g" -e "s/HAKU_CM/${CM}/g" -e "s/GIT_WRITE_SECRET/${GIT_WRITE_SECRET}/g" <<'EOF' | kubectl -n "$NS" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: HAKU_POD}
spec:
  restartPolicy: Never
  volumes: [{name: b, configMap: {name: HAKU_CM}}]
  containers:
  - name: git
    image: alpine/git
    command: ["sh","-c","set -e; git config --global credential.helper '!f(){ echo username=$GIT_USER; echo password=$GIT_PASS; };f'; git clone \"$REPO_URL\" /tmp/r; cd /tmp/r; git fetch /b/bundle refs/heads/main:refs/remotes/bundle/main; git push origin refs/remotes/bundle/main:refs/heads/main; echo HAKU_PUSH_DONE"]
    volumeMounts: [{name: b, mountPath: /b}]
    env:
    - {name: GIT_USER, valueFrom: {secretKeyRef: {name: GIT_WRITE_SECRET, key: username}}}
    - {name: GIT_PASS, valueFrom: {secretKeyRef: {name: GIT_WRITE_SECRET, key: password}}}
    - {name: REPO_URL, valueFrom: {secretKeyRef: {name: GIT_WRITE_SECRET, key: repo_url}}}
EOF

phase=""
for _ in $(seq 1 90); do
  phase="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  case "$phase" in Succeeded | Failed) break ;; esac
  sleep 2
done
logs="$(kubectl -n "$NS" logs "$POD" 2>&1 || true)"
echo "$logs"
git fetch origin -q
if echo "$logs" | grep -q HAKU_PUSH_DONE && [ "$(git rev-parse main)" = "$(git rev-parse origin/main)" ]; then
  echo "push_state: pod-fallback OK"
  [ "${CI_WAIT:-0}" = "1" ] && exec tools/ci_wait.sh
  exit 0
fi
echo "push_state: pod-fallback FAILED (see logs above)" >&2
exit 1
