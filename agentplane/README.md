# Agentplane

Agentplane runs coding agents in Kubernetes sandboxes and mediates what they do outside them:
network egress with credentials substituted at the proxy, operator-approved or policy-approved
Actions, and notifications. Its services are separately deployable; the Action Service supplies
the approval gateway for any MCP-capable LLM product, exposed externally through the standalone MCP edge.

**Design principles:** [`docs/principles.md`](docs/principles.md). Check new designs against it.

## Services

- [Action Service](action_service/README.md): ActionRequests, Decisions, auto-approval policies,
  upstream MCP execution and the gRPC/HTTP APIs used by the MCP edge.
- [MCP edge](mcp_edge/README.md): external Connection/OAuth authority and `/mcp`; forwards authenticated Actions calls over gRPC.
- [Runner Gateway](runner_gateway/README.md): one process providing runner state observation,
  authorized live Runner proxy, [Session Dispatcher](runner_gateway/dispatcher/README.md) durable queue,
  and `EnvironmentService` inventory (`ListEnvironments`, `WatchEnvironments`, immediate
  `GetEnvironment`, current-state `WatchEnvironment`), Sandbox creation/templates/management,
  and explicit resume through shared lifecycle machinery.
  New serving Pods run the queue migration before starting ([deployment](runner_gateway/README.md#deployment)).
- [History Service](history_service/README.md): retained Session history, read by the
  integration app and the Notification service.
- [Environment Runtime](env/README.md) and [native drivers](native/README.md): manage the Sandbox
  environment and run Claude Code or Codex through Runtime, which journals Session activity;
  Runtime delivers those journals to History.
- [Environment execution service](env/runtime/execution/README.md): typed process/filesystem API
  hosted by Environment Runtime.
- [Egress proxy](egress/README.md): authenticates sandbox workloads and substitutes credentials.
- [Policy Controller](policy_controller/README.md): expires `PolicyBinding`s and keeps the
  RoleBindings they grant.
- [Sandbox Controller](sandbox_controller/README.md): reconciles `Sandbox` Identity credentials,
  retained state PVCs and Pods or a KubeVirt VM.
- [LLM ingress](llm_ingress/README.md): authenticated hop in front of LiteLLM.
- [Notification service](notification_service/README.md): subscriptions and session inboxes.
- [Indexing](x/indexing/README.md): Git semantic search.
- [Integration app](app/README.md): the human-facing frontend over the services above, and a
  client of them. Its static browser shell has a separate [frontend image](app/frontend/serving/README.md).

A future SSH proxy for access to machines could use [sshpiper](https://github.com/tg123/sshpiper)
and its [Pipe CRD](https://github.com/tg123/sshpiper/blob/v1.6.1/plugin/kubernetes/crd.yaml).
The cluster is configured to retain that CRD through a [non-pruning Flux Kustomization](../cluster/cdk8s/sshpiper_crds/flux_kustomizations.py);
this does not imply an SSH proxy is currently deployed.

The opt-in [central code-mode sampler](central_sampler/README.md) keeps sampling, JavaScript
and continuation in a separate service and database; the Sandbox supplies only its executor.
Wrapped Codex and Claude continue to sample and execute inside their Sandbox. Staging enables the
optional cdk8s sampler with shared Responses routes and a separate direct-API selection. Existing
native Sessions retain their harness. The sampler selects direct Anthropic Messages API routes
independently; those direct models are not offered by the Codex or Claude harnesses. Responses calls
use LLM ingress with the projected workload token, while Anthropic calls use a separate mounted API
key and a host-pinned Cilium egress rule. The [k3s sampler scenarios](system_tests/README.md#central-sampler)
use the same workload construct, including its database migration and projected service-account credentials.

## Go service images

Go service images build with `pure = "on"` on the pinned
`gcr.io/distroless/static-debian13:nonroot` base (CA certificates and timezone data, no shell), with
explicit UID/GID `1000:1000` and working directory `/`. Images whose clients need the repository's CA
bundle add `//third_party/debian_slim:cacerts`. Environment Runtime uses the environment Nix images.

## System overview

Service boundaries and traffic are independent of environment or replica layout. The [system overview](docs/system_overview.md#system-overview) shows deployed services, traffic, database ownership and focused per-path diagrams.

## Implemented contracts

- [Action Service spec](action_service/SPEC.md) and [README](action_service/README.md): catalog,
  Decisions, events, bounded waits, cancellation, generic MCP, configured Identities, Connection
  grants, OAuth/consent.
- [Agent CLI](docs/agent_cli.md): the `agentplane` command ([`cli/`](cli/)) over the Action,
  Notification and Egress rules APIs; output, errors, exit codes, submitting and waiting.
- [App README](app/README.md): consent and Action-review presentation.
- [Sandbox Actions](action_service/sandbox/README.md): exec targets for agents hosted outside the
  cluster (sandboxes running as the calling ServiceAccount; Kubernetes reach from inside one).
- [Thread, runner, and harness layering](docs/thread_layering.md): command/Event architecture,
  Thread identity continuity, native recovery, optional app-first acceptance.
- [Thread view synchronization](docs/thread_view_sync.md): conversation model, sync-engine
  integration, on-demand Raw, history/catch-up, frontend state (distinct from the raw-replay API).
- [Executor liveness](docs/executor_liveness.md), [operator federation](docs/operator_federation.md),
  [workload authentication](docs/workload_authentication.md), [launch presets](docs/launch_presets.md),
  [action policies](docs/action_policies.md); the rest under [`docs/`](docs/).
