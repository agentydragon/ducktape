# Sandbox Actions: exec targets provisioned as the caller

Status: **design agreed (2026-09-18), not started.** No DAG nodes are minted yet; they belong with
the dispatch decision rather than with this note.

An Action group that provisions an Agentplane Sandbox and runs bounded commands in it. It exists
for callers outside the cluster — Claude Code web and claude.ai, bound by OAuth to the `claude-ai`
ServiceAccount — which have no Pod and so no other way to reach an in-cluster box.

These Sandboxes are not the integration app's. The app stamps one to _host an agent_: a runner
container, a harness, a Thread, a trajectory. This surface stamps one to _run commands in_, driven
by an agent that lives elsewhere. Same CRD, different operation, different component.

## The contract

**A sandbox is a shell as yourself.** It runs as the calling ServiceAccount, reaches what that
account's `EgressBinding`s allow, and — because a Pod proves its ServiceAccount, and the account
carries `agentplane.allegedly.works/use-action-service` — is admitted to the Action Service as that
same account. No caller obtains authority it did not already hold.

That answers the question an operator actually has when asked to bind this Action for a new
ServiceAccount: it is exactly as safe as that account's own authority, and granting it grants
arbitrary code execution under that identity.

The guard the rule rests on: the ServiceAccount comes from `ExecutionRequest.caller`, which the
authenticated admission path sets. It is never a tool argument. A surface accepting one would mean
"stamp a Pod as any account you can name".

## Why a code-owned executor

`ExecutionRequest` already carries `caller: ServiceAccountRef`, set by admission and not read by
the MCP executor. `ActionGroup.executor` (<../action_service/catalog.py>) is already a discriminated
union whose single member is `kind: Literal["mcp"]`, and `ActionGroup.actions` is a plain field an
MCP group fills from `tools/list` and a code-owned group declares from its own models. Adding the
kind is what that field shape anticipates, and
[durable SSH-backed processes](ssh_durable_processes.md) already commits to code-owned Actions for
its own reasons.

### Rejected: an MCP backend carrying the caller in per-call metadata

The pinned FastMCP has the seam — `Client.call_tool(meta=...)` and server-side
`Context.request_context.meta` — and it is the only per-call one. Arguments are validated against
the backend's advertised schema before the call and a backend may set `strict_input_validation`, so
an injected field is refused twice; headers belong to the transport, and the executor holds one
persistent connection per group.

What killed it is cost, not mechanism. It introduces a caller assertion on the shared authorization
path, which then has to be defended permanently: a tool argument named `caller` must not forge it,
envelope `origin`/`correlation` must never become it, and groups that did not ask for it must not
receive it. `request.caller` needs none of that.

### Rejected: forwarding the caller's own credential

There is no caller credential at dispatch. Submission and dispatch are separated by a human
Decision, a possible restart, and a possibly different replica, so a Pod-bound token or an OAuth
access token has expired or is simply absent. `ExternalGrantProvenance` holds issuer, client,
connection, grant and revision, and no token, by design.

Making one exist would mean retaining a credential in the request, which the service refuses, or
minting one per caller through TokenRequest, which would let the Action Service act as any account
that can call it.

### The cost, accepted

The Action Service's ServiceAccount gains Sandbox CRUD and `pods/exec` in the sandbox namespace,
and this code ships in its image and rollout.

An MCP backend would not have avoided the privilege. Such a backend cannot authenticate the caller
itself, so it can only trust the Action Service's static bearer — the bearer is then the capability,
one hop away. The split buys process isolation, not privilege isolation.

Exec runs in the Action Service process. Bounded `timeout_seconds` and `max_output_bytes` ceilings
keep a long build from streaming unbounded output into that Pod; the execution lease already renews
across a long call.

## One ServiceAccount per caller, not per sandbox

Every sandbox this surface stamps runs as the caller's account. The integration app does the
opposite — an account per Sandbox, owner-referenced to it — and the difference is deliberate, with
intended consequences.

**A sandbox is a caller.** It can provision and exec sandboxes itself, including ones it did not
create, because they are one principal. The operation is closed: no chain gains authority.

**Egress attribution collapses per principal.** `DecisionsClient.recent` (<../app/decisions.py>)
asks the proxy for decisions _by ServiceAccount_, and a `Decision` carries no Pod field, so two
sandboxes of one account are indistinguishable there and `GET /sandboxes/{name}/egress/decisions`
returns one combined stream for each of them. Accounts still differ from each other. This knowingly
deviates from the [egress specification](../egress/SPEC.md)'s "bind only an account dedicated to one
workload".

**The workload must never read the token.** `automountServiceAccountToken: false`, with the
projected audience-scoped token mounted by the egress sidecar alone, is what keeps a shared account
out of the container an agent runs commands in. Every template this surface stamps holds that
invariant; breaking it now costs the whole account's authority rather than one box's.

### Rejected: standing bindings on the caller, mirrored onto per-sandbox accounts

The alternative that keeps per-sandbox accounts: read the caller's `EgressBinding`s and write
derived ones onto each new account.

It breaks a property the egress specification states plainly — "creating one is the whole act of
allowing, and deleting it the whole act of taking that back" — because deleting the source takes
back nothing already copied. Kubernetes cannot repair that: garbage collection deletes a dependent
when its _last_ owner goes, and revocation needs the _first_. Renewal by the provisioning component
is already rejected in [egress composition](../docs/egress_composition.md) for putting that
component back in the enforcement path. What is left is a bounded expiry on every derived binding,
and revocation complete only within that window.

Running as the caller needs none of it: the account the proxy authenticates is the account the
bindings are on.

## Separation from the integration app

**Its own label and selector.** Not `agentplane.allegedly.works/managed`, which the app lists,
watches and gates operations on (<../app/inventory.py>, <../app/live.py>). An exec target carrying
it joins the app's fleet view and its ingestion coordinator, which then discovers a runner the box
does not have.

**Its own template set**, reviewed configuration selected by name at the same operator cadence as
`action_groups`. Not a free-form argument, which would let a caller stamp any template in the
namespace, the app's runner template included.

**Its own inventory code**, reusing the CRD shapes rather than `SandboxInventory`, which hardcodes
the per-sandbox account, the app's label and the app's ownership model.

An exec target needs a workload container, the egress sidecar, the CA bundle and the proxy
environment; no runner, no state volume, no model wiring. Stamping the app's runner template works
for a first smoke test and is the wrong destination: its workload container is the runner image, so
a command runs inside a harness process's container.

## Kubernetes from inside the box

The box reaches the API server the way it reaches everything else: a placeholder in its kubeconfig,
an `EgressCredential` the central proxy substitutes, and a rule admitting `kubernetes.default.svc`.
The workload holds no Kubernetes credential, exactly as it holds no Forgejo one.

The gap is the audience. The only token in play is projected with the proxy's own
(`agentplane-egress`, <../../../cluster/cdk8s/agentplane/llm_ingress_constructs.py>), which is what
`authenticatedWorkloadToken` retains and substitutes, and the API server validates against its own
`--api-audiences`, so that bearer is refused there.
[Workload authentication](../docs/workload_authentication.md) already defers exactly this as
"Kubernetes API access under a distinct projected audience".

**The sidecar projects a second token.** A `serviceAccountToken` source carrying the API server's
audience, mounted by the sidecar alone and rotated by kubelet, presented for the proxy to substitute
into `Authorization` on the rules that name it. The proxy gains no Kubernetes rights, the workload
sees neither token, and the container boundary holding it is the one the design already rests on
([identity evidence](../docs/sandbox_egress_identity_evidence.md)). Before substituting, the proxy
reviews that token against the API server audience and requires its subject to equal the subject it
authenticated, so a substituted credential provably belongs to the caller being decided.

RBAC is then standing on the calling account, beside its `EgressBinding`s, and every sandbox of that
account has it. Under the contract above that is the intent rather than a leak, and it is much less
machinery than `SANDBOX_RBAC` assumes: no per-sandbox grant, no reconciler, no orphan cleanup.

### Rejected: the proxy mints one per request

`TokenRequest` for the account that just authenticated, bound to the Pod its review named. It needs
`create` on `serviceaccounts/token` in every namespace the proxy accepts bearers from, on the one
component already holding every substituted credential — the grant shape `EGRESS_SOURCE_ADDRESS`
removed the proxy's `pods` read to avoid.

### Rejected: mounting the token in the workload container

The simplest source, and it gives up what the placeholder design exists for: a credential inside the
box can be copied out and used elsewhere until it expires, where substitution confines the identity
to requests the proxy admitted. "A shell as yourself" bounds authority inside the box; it does not
extend to a portable credential for that account. It also breaks the sidecar-only projection above.

### What to verify rather than assume

- `kubernetes.default.svc` resolves to a private address, so its rule declares `clusterInternal` or
  the proxy refuses it whole.
- The proxy bumps TLS, so `kubectl` must trust the egress CA. The Haku box already writes a
  kubeconfig whose `certificate-authority` names that bundle.
- Ordinary requests should pass, but `exec`, `attach` and `port-forward` upgrade to SPDY or
  WebSocket through a bumping proxy and a watch streams chunked. Haku's own API proxy answers `501`
  to the upgrade verbs, so this is where to expect trouble.

## Tool surface

`provision`, `exec` and `list`, with `info` and `dispose`. Shapes follow
<../../../haku/console/tools/sandbox.py>, the surface already in daily use: one bounded Bash script
per call, a per-environment ceiling on timeout and retained output patched into the advertised
schema, and a nonzero exit reported as a result rather than a transport error.

Properties of the Action path the tool documentation has to state, because an agent assuming
otherwise misreads every call:

- submission is non-blocking and a receipt is not execution success; the caller polls the durable
  event sequence to a terminal state;
- nothing is retried after dispatch, which is what a command with side effects needs, and
  `execution_unknown` is reported as itself rather than as a failure;
- an `ActionPolicySet` and a binding for the calling account are the access control. Without them
  every command waits for a human, which is not an exec loop.

## Work

1. **The second executor kind.** Widen the union, validate the binding, let a group declare its
   Actions statically. Independently reviewable, and what `ssh_durable_processes.md` needs.
2. **The sandbox executor.** Provision, exec, list, info and dispose against Sandboxes running as
   `request.caller`, under its own label, from its own template set, refusing anything it did not
   create.
3. **Group configuration, the auto-approval binding for `claude-ai`, and that account's
   `EgressBinding`s.** Provable end to end from a claude.ai session against an existing template.
4. **A dedicated exec-target template**, and the Forgejo `EgressCredential` a box needs to check out
   `haku-state`.
5. **Kubernetes from the box.** The second projected audience in sidecar and template, the
   subject-matched substitution in the proxy, the credential and rule, and the calling account's
   RoleBindings. Its own clock: nothing above waits on it.

## Not here

- **A production tier.** Staging only.
