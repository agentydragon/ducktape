# Multi-endpoint Services need `xt_statistic` in the RBE guest kernel

The central-sampler two-replica rolling fixture exposed a kernel prerequisite in the
bare-k3s RBE action on 2026-10-31. This is a system-test infrastructure failure,
before the sampler-roll scenario reaches its provider barrier or changes the Deployment.

## Observations

- The focused `test_mcp` run [534a125f](https://app.buildbuddy.io/invocation/534a125f-47ba-4560-aec5-1e0c65fc52ef)
  had two Ready sampler Pods. It created a Ready Sandbox, then timed out waiting for
  the `session-dispatcher` ClusterIP gRPC channel. No provider request or roll occurred.
- Its retained `test.outputs/k3s.log` repeatedly reports kube-proxy
  `Failed to execute iptables-restore`, with
  `Warning: Extension statistic revision 0 not supported, missing kernel module?`.
  Kube-proxy retries Service-rule synchronization every 30 seconds. The
  `session-dispatcher` Service selects the Ready Runner Gateway; the Gateway's log
  shows its database migration completed.
- [Chart PR #13243's hosted CI](https://app.buildbuddy.io/invocation/372aeb74-5e84-5a6a-a8b4-319b4c7cb2b9)
  independently records the same `statistic` error in both `test_mcp` and
  `test_vm` k3s logs. Its existing central-sampler MCP case also times out at
  dispatcher ClusterIP readiness. The earlier [central-sampler investigation](../../../debug/central_sampler_v1.md)
  recorded this failure when a Gateway Service temporarily had multiple
  endpoints, before the current sampler change.

Kube-proxy's iptables mode uses the `statistic` match to distribute a
multi-endpoint Service. A failed `iptables-restore` prevents its whole Service
ruleset from synchronizing, so an unrelated single-endpoint Dispatcher Service
can fail too. The action's guest kernel must provide
`CONFIG_NETFILTER_XT_MATCH_STATISTIC` as built-in support or an available module.
The log proves the match is unavailable in this action; it does not distinguish
an unset kernel option from a missing or unloadable matching module. The RBE
OCI image is mounted as the guest root filesystem, so adding user-space
iptables packages alone cannot supply kernel match support.

## Acceptance boundary

The true two-replica rolling acceptance remains unproven. Keep the
[rollout scenario PR #13246](https://github.com/agentydragon/ducktape/pull/13246)
draft. Rerun the focused case and the affected MCP/VM suites on an RBE guest
with the `statistic` match, then inspect provider, old/new Pod, lease and
History evidence. Do not infer a sampler regression or a successful roll from
the present dispatcher timeout. A longer timeout, headless Service or direct
Pod-IP connection would avoid the intended ClusterIP multi-endpoint behavior.
