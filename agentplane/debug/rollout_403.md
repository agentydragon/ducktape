# Testing rollout 403

The intermittent rejection's exact cause remains unproven. Commit `41be8e0aaf` records the observed
response as Envoy `text/plain` `Access denied` during an app rollout gap. It records no request
timestamp, caller identity, source node or selected Gateway. The current app returns JSON for its
own authentication/authorization failures; its TokenReview allowlist rejects a successfully
authenticated username with HTTP 403.

Read-only checks on 2026-10-14 around 06:00 UTC found the testing app Ready on image `86568b5`, with
`AGENTPLANE_TOKEN_SUBJECTS` accepting only
`system:serviceaccount:agentplane-testing:agentplane-agent`. The current app Pod had no 403 or
TokenReview refusal in its lifetime. Retained Loki queries over `2026-10-13T21:00:00Z` through
`2026-10-14T06:00:00Z` returned no matching app rejection, no proxy log naming the testing public
origin, and no `Access denied` log in `kube-system` or `gateway-system`. A broad `403` search matched
the Kubernetes flow-schema UID in unrelated response headers, not HTTP 403 events.

[The local Gateway policy check](../../cluster/docs/cilium_network_policy.md#egress-through-a-local-gateway)
can produce the same response: backend selection evaluates the local source Pod's policy against
the backend identity and target port. That is a lead, not evidence that this request took that path.
Do not change retries or timeouts to treat it as a diagnosed availability failure.

The next occurrence needs a correlated timestamp, status/content type, `Server` header and public
destination IP, plus the caller's source Pod/node and proxy path when applicable. Collect no bearer,
cookies, request bodies or raw TokenReview error text. Correlate the app's fixed TokenReview reason
and authenticated username with the Gateway's selected backend and Cilium policy verdict at that
time. Without the original request or a bounded reproduction on the original caller path, retained
logs cannot determine whether this was a same-node policy check or another Envoy rejection.
