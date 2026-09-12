# Staging evidence

Observations on the staging Agentplane deployment on 2026-09-12, read back through the Action
Service's own APIs unless attributed to the operator. They are the record behind the completed
external client, operator approval, and Web Push work; the deployed proof of the Sandbox path is
the [acceptance suite](../acceptance/README.md).

## Deployment

Action Service image
`git.allegedly.works/ducktape-ci/agentplane-action-service:devel-20260912122305-e2303e4`,
Deployment `agentplane-staging/agentplane-actions` at generation 73 with two ready, updated
replicas, serving the MCP endpoint externally (12:57:45Z).

## External Connection through Claude.ai

The MCP facade accepted an OAuth-enrolled Connection (`088a0679-f6e3-4f97-b63c-c9fce3fc84ad`, grant
revision 1, client `https://claude.ai/oauth/mcp-oauth-client-metadata`) bound to ServiceAccount
`agentplane-staging/claude-ai`. `get_action_policy(target="self")` returned binding
`claude-ai-github-reads` (resourceVersion 286352196) with unexpired sets `github-reads` and
`github-identity-reads` (generation 1 each).

Two requests were auto-approved by that binding (Decision provider `action_policy_set`, reason
`policy_set_auto_approve`, `policy_evidence` naming the binding and the matching `exact_actions`
policy of `github-reads`) and executed through the operator-linked GitHub upstream:

| Request                                | Action               | Created (UTC)   | Execution                                             |
| -------------------------------------- | -------------------- | --------------- | ----------------------------------------------------- |
| `da669074-b55d-4b6f-a59d-784413c1e405` | `github/get_me`      | 12:57:45.264395 | succeeded 12:57:46, the linked account `agentydragon` |
| `1e780054-0f39-416e-b3d6-bb75edcfb3f3` | `github/search_code` | 12:58:33.111358 | succeeded 12:58:38, a code search result page         |

Resubmitting a used idempotency key was refused ("already used by this caller"), and
`get_action_request(idempotency_key=…)` recovered the original request.

## Human operator approval

`github/create_branch` (request `d7e43a95-4dc3-4015-9895-7179dbd71fec`, created 12:58:16.505543Z;
arguments `agentydragon/ducktape`, branch `test-branch` from `devel`) waited in `decision_pending`
until the operator allowed it on the integration app's Actions page: Decision provider
`human_operator`, issuer the Authentik `agentplane-actions` application, decided 13:03:12.977810Z;
events `allowed`, `dispatching`, `running`, `succeeded` (13:03:14.902620Z); the Execution result
names `refs/heads/test-branch` at `9392be47`.

The local-Gateway TLS reset that had made operator federation fail intermittently is fixed
cluster-wide
([root cause and rollout](../../../cluster/debug/agentplane_oidc/local_gateway_tls_rca.md)); a
federation failure now logs its cause.

## Web Push

Operator-reported: a browser push for a pending Action arrived, and the request was decided from
the notification's buttons. The push arriving is the deployed VAPID identity and push-service
egress working.

## Not exercised

Deliberately left to bug reports rather than further acceptance runs: the app's state update after
a notification-button decision; duplicate Decision or Execution under retries, refresh, reconnect,
or an already-decided request; push subscription revocation; unavailable-push fallback to the app
UI; SSE fallback under push-service unavailability; Web Push reconciliation after
disconnect/reconnect.

Still open work, tracked in the plans: the Deny control from the Actions page or a notification;
grant retention across token refresh and service or client restart, negative isolation
(wrong-resource or invalid tokens, disabled Identities, unbound Connections, another Identity), and
unbind/revocation for an external client; refresh, token rotation, and degraded/reconnect behavior
of the linked GitHub upstream; the Kubernetes upstream.
