# Agentplane egress identity 502 investigation (2026-09-15)

## Finding

The failed Claude late-binding acceptance case stopped before its GitHub probe. In
the same window, central egress denied requests to testing's LLM ingress with no
resolved Sandbox identity. That is consistent with the failed case being unable to
reach its model endpoint, but null identity fields prevent correlating those denials
directly to the acceptance sandbox. The retained evidence points to a Kubernetes
control-plane availability incident as a likely contributor to identity lookup
failures. It does not prove which API request failed or tie a specific API-server
timeout to an egress request.

This is not evidence of a LiteLLM or model-vendor failure. The observed egress
decision was for the internal LLM-ingress service and had no Sandbox identity.

## Evidence

- BuildBuddy invocation
  [`92f9cd4c`](https://app.buildbuddy.io/invocation/92f9cd4c-2f08-4914-b14d-cc343381b0b0)
  failed `test_a_policy_granted_after_the_sandbox_is_running_takes_effect[claude]`
  after 831.99 seconds. The failure was the _initial_ expected-denial check for
  `github.com`; the decision history was empty. The later policy grant and retry
  never ran.
- Retained Agentplane events for sandbox `accept-bind-1-upyrf` record command
  admission, input confirmation, and a terminal `TURN_STATUS_FAILED` with an HTTP
  502 diagnostic.
- Loki logs from `agentplane-testing` show central egress denying three POSTs to
  `agentplane-llm-ingress.agentplane-testing.svc.cluster.local:8080` at
  08:01:34, 08:02:35, and 08:03:36 UTC. Each decision has `reason=unavailable`;
  `sandbox_namespace`, `sandbox_uid`, `source_pod_uid`, and `sandbox` are all null.
  Those records are consistent with failure to establish workload identity before
  policy evaluation, but cannot be attributed to the named acceptance sandbox.
- The sampled Loki query does **not** show three `ApiException` logs from the
  egress pod. The `(504)` `ApiException` messages in that window are from the
  Agentplane app/actions Kubernetes watchers in `agentplane-staging`, not from the
  `agentplane-testing` egress pod. Keep these as separate observations.
- Kube-apiserver logs in the same window contain `http: Handler timeout`, etcd
  request timeouts, and canceled/deadline-exceeded etcd `KV/Range` calls. This is
  independent evidence of control-plane/etcd trouble, but there is no request ID,
  audit record, or other correlation tying one of those failures to the egress
  identity lookup.

## Visibility limit and next evidence

At the failed revision, workload resolution called both Kubernetes TokenReview
and Pod GET; non-404 API exceptions from either propagated without operation-level
logging. The retained logs therefore cannot distinguish TokenReview from Pod GET
as the failing request. The available API-server messages also omit enough request
context to make that distinction retrospectively.

The shared resolver now logs a fixed operation name and numeric status for
TokenReview failures, without bearer data or exception bodies. This diagnostic is
deployed in staging, but post-deployment logs cannot identify the historical
failure. If it recurs, preserve the egress operation/status together with a safe
request correlation ID and Kubernetes API-server health evidence. Do not relax the
fail-closed identity check. A targeted fault-injection acceptance test could
exercise the unavailable path separately from waiting for another live incident.
