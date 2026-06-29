# kagent — status

**Suspended 2026-05-08.** All Flux kustomizations except `kagent-namespace` are
suspended (app, crds, db, secrets). Namespace and resources deleted from cluster.
Reason: kagent lacks tool-call output truncation/limiting — a
single MCP call (`kubectl get events`, full pod listings) dumps enough text
into the event history that the next request exceeds z.ai's per-prompt cap
(error 1261), killing the session. This makes the platform too fragile for
cluster-ops use. See the "Tool-output robustness" section in
<../../../../docs/self_hosted_coding_agent_platforms.md>.

## Rough edges (don't expect production-quality)

- **No client-side context budget.** kagent has no token-counting or
  pre-truncation. Tool outputs from a single MCP call (`kubectl get events`,
  full pod listings, etc.) routinely vomit enough text into the event history
  that the next request blows past z.ai's coding-plan per-prompt cap
  (error `1261 - Prompt exceeds max length`). GLM-5.1 itself supports 200K,
  but the coding plan caps each prompt much lower (z.ai doesn't publish the
  number; empirically ~32–64K).
- **Compaction is interval-based, not budget-based.** v0.9.x exposes
  `Agent.spec.declarative.context.compaction.{compactionInterval,overlapSize,summarizer}`
  (Google ADK under the hood). It summarizes _between_ user turns. It does
  not help when a single turn's tool calls already exceed the cap, which is
  the failure mode actually observed here, so we did not enable it. Adds
  latency + a summarizer call per turn for limited benefit against the real
  problem.
- **Stock agents ship with huge tool sets.** The k8s-agent system prompt +
  MCP tool schemas alone are tens of thousands of tokens before any tool
  call runs, leaving very little budget for actual tool outputs.

## Followups, in increasing effort

1. Pin agents to a smaller tool subset (per-agent override of the chart's
   tool list — needs investigation; the chart subcharts under
   `helm/agents/<agent>/` may expose a values knob).
2. Add a tool-output truncator at the controller level (would require an
   upstream PR; ADK doesn't expose this knob today).
3. Switch to a model/plan with a higher per-prompt cap (general
   `/api/paas/v4` endpoint at PAYG rates, or a different vendor entirely).
4. Wait for upstream kagent to add a token-budget-aware compaction trigger.

## Decommission (later)

- [ ] **Delete kagent from the cluster** — CRDs, DB (`kagent-db`), secrets, the
      `devbot` manifests, and the suspended Flux kustomizations — unless kagent
      fixes tool-output truncation / large-MCP-output handling upstream, or the
      project becomes active again. The platform is parked and not in use; the
      manifests are dead weight until/unless that changes. Revisit on each kagent
      upstream release.

## See also

- <namespace/namespace.yaml> — PSS dropped to `baseline` (TODO to retighten
  to `restricted` once upstream agent Deployments set proper securityContext).
- <../../../../docs/zai_api.md> — z.ai endpoint shapes, including the coding
  plan vs general distinction.
- <../../../../docs/self_hosted_coding_agent_platforms.md> — broader survey of
  options if kagent's rough edges become disqualifying.
