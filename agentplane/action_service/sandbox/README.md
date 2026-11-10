# Sandbox Actions: exec targets that run as the caller

An Action group that creates a box, a plain Pod running as its caller, and runs bounded commands in
it. It exists for callers outside the cluster — Claude Code web and claude.ai, bound by OAuth to a
ServiceAccount — which have no Pod and so no other way to reach an in-cluster box, and for agents in
Session Sandboxes that want a box of their own.

A box is not a Session Sandbox ([ADR](../../docs/adr_action_sandboxes.md)). The app requests a
Session Sandbox to _host an agent_: a runner, a state volume, an account of its own. This
surface creates a box to _run commands in_, driven by an agent that lives elsewhere. They share the
Pod recipe (`cluster/cdk8s/agentplane/sandbox_pod.py`) and nothing else.

## The contract

**A box runs under its caller's ServiceAccount.** The account is the Pod's execution subject and
the subject of the box's own `WorkloadCredential`, which the service requests per box, named after
its Pod and bound to it ([the record contract](../../docs/workload_authentication.md#workloadcredentials)).
The issued Secret is mounted into the egress sidecar alone, which presents it to the proxy and the
egress rules route; policy remains the caller's `PolicyBinding`s. Workload containers receive only the local proxy address and public trust bundle.
They mount neither a token nor an SVID.

The guard the rule rests on: the ServiceAccount comes from `ExecutionRequest.caller`, which the
authenticated admission path sets. It is never a tool argument. A surface accepting one would mean
"stamp a Pod as any account you can name".

The Action Service's own ServiceAccount creates, reads and deletes Pods and holds `pods/exec` in the
sandbox namespace, and exec runs in the Action Service process. `timeout_seconds` and `max_output_bytes`
ceilings keep a long build from streaming unbounded output into that Pod, and the command's own
ceiling is the only bound on how long it runs: the executor renews the execution lease underneath
it, so a call lasting minutes is ordinary rather than an attempt the sweep reclaims.

## One ServiceAccount per caller, not per sandbox

Every sandbox this surface stamps for a ServiceAccount caller runs as the caller's account. A Session
Sandbox has its own owner-referenced account, and the difference is deliberate, with
intended consequences. A caller whose SVID names an Identity has no account: its boxes run on the
namespace's `default` ServiceAccount, and only a group of the policy namespace, where the Identity
lives, serves it.

**The workload must never read a ServiceAccount credential.** Every template sets
`automountServiceAccountToken: false`. The inventory refuses projected tokens mounted in workload
containers, and templates naming the SVID's volume or mount path. The projected tokens and the SVID
are mounted only in the sidecar.

## Separation from the integration app

**Its own label and selector.** Not `agentplane.allegedly.works/managed`, which identifies Session
Sandboxes that carry runners (<../../runner_gateway/environments/internal/management/access.go>). A box carrying it would join the
fleet view, which then looks for a runner the box does not have.

**Owned through its credential.** Each box's Pod is owned by its `WorkloadCredential`, and the
record by its caller's Session Sandbox, the controller of the account an agent in it calls this
surface as, so the boxes it creates are garbage-collected with the Sandbox. A static account's records
are owned by the account, and its boxes last until `dispose`. An Identity's records name and are owned
by the Identity incarnation, so its boxes go with the Identity. The record precedes its Pod because
the Pod names the record's Secret when it is created, so the Pod cannot own the record.

**Its own template set**: a caller names a template, but only one the group's reviewed
configuration offers, at the same operator cadence as `action_groups`, with the description an agent
chooses it by. Each names a core `PodTemplate` in the namespace that cdk8s renders from the shared
recipe (<../../../cluster/cdk8s/agentplane/command_sandbox.py>): an object rather than a Pod spec in
the configuration, so the image pins rewrite its containers. Staging offers a command box on the
plain sandbox image and a build-sized box. The Action Service refuses to start on an offered template
that is missing or lets the workload mount a ServiceAccount token, and
checks again at each `create`. A command runs in the container `kubectl exec` would pick, the Pod's
`kubectl.kubernetes.io/default-container` or else its first. `get_template` returns one offered
template's Pod whole. **Every caller can read everything in an offered template**, so a template
references Secrets and never inlines one.

## Kubernetes from inside the box

The sidecar offers a separate Kubernetes-audience projected token for policy-authorized
substitution. Commands use the Kubernetes credential's host, placeholder and invocation from the
rules response; no kubeconfig or token is mounted in the workload. Kubernetes RBAC still applies to
the caller's own ServiceAccount.

## Tool surface

`create`, `exec` and `list`, with `get`, `get_template` and `dispose`. `create` returns once the
Pod exists and the caller polls `get` for the Pod's phase and conditions.

**Conditions are passed through as Kubernetes wrote them**, not summarised into a state. A reading
taken here would be a second opinion that carries less than the original: `PodScheduled` with
`Unschedulable` and `Ready` with `ContainersNotReady` are different answers to "why is this box not
ready", and only their own `reason` distinguishes them.

**`create` does not wait for readiness.** An image pull or a full node can hold a box unready for
longer than a call should wait, where `get` reports why.

**A full namespace quota refuses `create` outright**, with the API server's own message: the caller's
cue to `list` its sandboxes and `dispose` the ones it has finished with. A box evicted from its node
stays `Failed`; nothing recreates it, so `dispose` + `create` is how a caller gets it back.

`exec` takes one bounded Bash script per call: the deployment's ceiling on timeout and retained
output is patched into the advertised schema, and a nonzero exit is a result rather than a
transport error.

Properties of the Action path the tool documentation has to state, because an agent assuming
otherwise misreads every call:

- submission is non-blocking and a receipt is not execution success; the caller polls the durable
  event sequence to a terminal state;
- nothing is retried after dispatch, which is what a command with side effects needs, and
  `execution_unknown` is reported as itself rather than as a failure;
- a `PolicySet` and a `PolicyBinding` for the calling account are the access control. Without them
  every command waits for a human, which is not an exec loop.

Staging also offers each of them to external Connections as a direct tool (`sandbox__exec` and so on;
[Action Service README](../README.md) § Direct tools): a call answers with its result,
the model as structured content and as JSON text, or with its request once 30 seconds have passed,
for `get_action_result` to wait on. A command that outlives the wait keeps running; answering with
the request rather than holding the call open keeps a client that gives up first from running the
script again.

## Rejected alternatives

**An MCP backend carrying the caller in per-call metadata.** The pinned FastMCP has the seam —
`Client.call_tool(meta=...)` and server-side `Context.request_context.meta` — and it is the only
per-call one. What killed it is cost, not mechanism: it introduces a caller assertion on the shared
authorization path, which then has to be defended permanently: a tool argument named `caller` must
not forge it, and groups that did not ask for it must not receive it. `request.caller` needs none of
that. Such a backend would also not avoid the privilege: it cannot authenticate the caller itself,
so it can only trust the Action Service's static bearer, and the bearer is then the capability one
hop away. The split buys process isolation, not privilege isolation.

**Forwarding the caller's own credential.** There is no caller credential at dispatch. Submission
and dispatch are separated by a human Decision, a possible restart, and a possibly different
replica, so a Pod-bound token or an OAuth access token has expired or is simply absent. Making one
exist would mean retaining a credential in the request, which the service
refuses, or minting one per caller through TokenRequest, which would let the Action Service act as
any account that can call it.

**Standing bindings on the caller, mirrored onto per-sandbox accounts.** The alternative that keeps
per-sandbox accounts: read the caller's `PolicyBinding`s and write derived ones onto each new
account. It breaks a property the egress specification states plainly — "creating one is the whole
act of allowing, and deleting it the whole act of taking that back" — because deleting the source
takes back nothing already copied. Kubernetes cannot repair that: garbage collection deletes a
dependent when its _last_ owner goes, and revocation needs the _first_. Renewal by the provisioning
component is already rejected in [egress composition](../../docs/egress_composition.md) for putting that
component back in the enforcement path. What is left is a bounded expiry on every derived binding,
and revocation complete only within that window.

**The proxy mints a Kubernetes token per request.** `TokenRequest` for the account that just
authenticated, bound to the Pod its review named. It needs `create` on `serviceaccounts/token` in
every namespace the proxy accepts bearers from, on the one component already holding every
substituted credential — the per-namespace grant the proxy declines even for a `pods` read
([egress README](../../egress/README.md#rejected-binding-a-bearer-to-its-pods-source-address)).

**Mounting the Kubernetes token in the workload container.** The simplest source, and it gives up
what the placeholder design exists for: a credential inside the box can be copied out and used
elsewhere until it expires, where substitution confines the identity to requests the proxy admitted.
"A shell as yourself" bounds authority inside the box; it does not extend to a portable credential
for that account. It also breaks the sidecar-only projection above.
