#!/bin/sh
set -eu
# pipefail makes a curl-failure exit status propagate through the awk filter,
# so the Job actually fails (and the pod retries) instead of looking healthy.
# busybox ash supports `set -o pipefail` since 1.34.
set -o pipefail
# Sizes are the model-layer size from the ollama registry manifest.
# Total ~116 GB; PVC `llm-models` is 200Gi.
#
# We hit `/api/pull` directly instead of using `ollama pull` because the CLI
# emits a CR-redrawn TTY progress bar that is unreadable in `kubectl logs`.
# Streaming JSON lets us print one human-readable line per status change and
# every ~5% of bytes.

# Pod can race ollama's readiness on a co-recreate; wait for the API.
echo "=== waiting for $OLLAMA_HOST ==="
until curl -sSf -m 5 "$OLLAMA_HOST/" >/dev/null 2>&1; do
  sleep 5
done
echo "=== ollama is up ==="

pull() {
  model=$1
  echo "=== pulling $model ==="
  curl -sS -N --fail-with-body -X POST "$OLLAMA_HOST/api/pull" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$model\",\"stream\":true}" \
    | awk -v model="$model" '
        BEGIN { last_status=""; last_pct=-1 }
        {
            status = ""; total = 0; comp = 0; err = ""
            if (match($0, /"status":"[^"]*"/))    status = substr($0, RSTART+10, RLENGTH-11)
            if (match($0, /"total":[0-9]+/))      total  = substr($0, RSTART+8,  RLENGTH-8) + 0
            if (match($0, /"completed":[0-9]+/))  comp   = substr($0, RSTART+12, RLENGTH-12) + 0
            if (match($0, /"error":"[^"]*"/))     err    = substr($0, RSTART+9,  RLENGTH-10)
            if (err != "") { printf "[%s] ERROR: %s\n", model, err; exit 1 }
            if (total > 0) {
                pct = int((comp*100)/total)
                if (status != last_status || pct - last_pct >= 5 || pct == 100) {
                    printf "[%s] %s: %3d%% (%.2f / %.2f GB)\n", model, status, pct, comp/1e9, total/1e9
                    last_status = status; last_pct = pct
                }
            } else if (status != "" && status != last_status) {
                printf "[%s] %s\n", model, status
                last_status = status
            }
        }
    '
}

pull gpt-oss:20b        # 13.8 GB
pull gpt-oss:120b       # 65.4 GB
pull gemma4:31b-it-q8_0 # 33.8 GB
pull qwen3-embedding:4b # 2.5 GB
