#!/usr/bin/env bash
# Send one request per API shape through the scratch LiteLLM, then report, per trace, which
# spans carry the input and the attribution keys, and whether the content reached pod logs.
set -euo pipefail
NS=scratch-langfuse-tracing
RUN=${1:?usage: [STREAM=true] probe.sh <label>}-$(date +%s)
STREAM=${STREAM:-false}

secret() { kubectl -n "$NS" get secret scratch-secrets -o jsonpath="{.data.$1}" | base64 -d; }
KEY=$(secret litellm-master-key)
kubectl -n "$NS" port-forward svc/litellm 14000:4000 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF' EXIT
timeout 60 sh -c 'until curl -sf localhost:14000/health/liveliness >/dev/null; do sleep 1; done'

# Attribution as Agentplane's LLM ingress sends it (agentplane/llm_ingress/README.md): the
# x-litellm-spend-logs-metadata JSON header plus W3C baggage. Props correlation rides in the body.
meta() { printf '{"trace_metadata":{"props.marker":"%s"}}' "$1"; }
post() {
  curl -sS -o /dev/null -w "$1 %{http_code}\n" localhost:14000"$1" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -H "x-litellm-spend-logs-metadata: {\"agentplane.namespace\":\"probe-ns\",\"agentplane.session_id\":\"$RUN\"}" \
    -H "baggage: litellm.metadata.spend_logs_metadata.agentplane.namespace=probe-ns,litellm.metadata.spend_logs_metadata.agentplane.session_id=$RUN" \
    -d "$2"
}
post /v1/responses "{\"stream\":$STREAM,\"model\":\"luna-responses\",\"input\":\"Repeat exactly: $RUN-responses\",\"litellm_metadata\":$(meta "$RUN-responses")}"
post /v1/messages "{\"stream\":$STREAM,\"model\":\"luna-messages\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"Repeat exactly: $RUN-messages\"}],\"litellm_metadata\":$(meta "$RUN-messages")}"
post /v1/chat/completions "{\"stream\":$STREAM,\"model\":\"ministral-chat\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"Repeat exactly: $RUN-chat\"}],\"metadata\":$(meta "$RUN-chat")}"

CH_PASSWORD=$(secret clickhouse-password)
ch() { kubectl -n "$NS" exec deploy/clickhouse -- clickhouse-client -u langfuse --password "$CH_PASSWORD" -q "$1"; }
for _ in $(seq 60); do
  [ "$(ch "SELECT count() FROM langfuse.events_full WHERE position(input, '$RUN') > 0")" -ge 3 ] && break
  sleep 5
done
ch "SELECT substring(trace_id, 1, 8) AS trace, name, parent_span_id = '' AS root,
       position(input, '$RUN') > 0 AS has_input, length(output) > 0 AS has_output,
       arrayFilter(n -> n LIKE '%agentplane%' OR n LIKE '%props%', metadata_names) AS attribution
    FROM langfuse.events_full
    WHERE trace_id IN (SELECT trace_id FROM langfuse.events_full WHERE position(input, '$RUN') > 0)
    ORDER BY trace_id, start_time FORMAT PrettyCompactMonoBlock"
echo "pod-log lines containing the probe content: $(kubectl -n "$NS" logs deploy/litellm --since=10m | grep -c "$RUN" || true)"
