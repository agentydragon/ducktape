# Sandbox Actions: exec targets that run as the caller

An Action group that stamps an Agentplane Sandbox and runs bounded commands in it. It exists for
callers outside the cluster — Claude Code web and claude.ai, bound by OAuth to a ServiceAccount —
which have no Pod and so no other way to reach an in-cluster box.

These Sandboxes are not the integration app's. The app stamps one to _host an agent_: a runner
container, a harness, a Thread. This surface stamps one to _run commands in_, driven
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

The Action Service's own ServiceAccount holds Sandbox CRUD and `pods/exec` in the sandbox
namespace, and exec runs in the Action Service process. `timeout_seconds` and `max_output_bytes`
ceilings keep a long build from streaming unbounded output into that Pod, and the command's own
ceiling is the only bound on how long it runs: the executor renews the execution lease underneath
it, so a call lasting minutes is ordinary rather than an attempt the sweep reclaims.

## One ServiceAccount per caller, not per sandbox

Every sandbox this surface stamps runs as the caller's account. The integration app does the
opposite — an account per Sandbox, owner-referenced to it — and the difference is deliberate, with
intended consequences.

**A sandbox is a caller.** It can create and exec sandboxes itself, including ones it did not
create, because they are one principal. The operation is closed: no chain gains authority.

**Egress attribution collapses per principal in the app's view, not in the record.**
`DecisionsClient.recent` (<../app/decisions.py>) asks the proxy for decisions _by ServiceAccount_
and projects a `Decision` with no Pod field, so `GET /sandboxes/{name}/egress/decisions` returns one
combined stream for every sandbox of one account. The proxy's own record does carry
`source_pod_uid`, persisted with the rest (<../egress/decisions.py>), so per-box attribution is a
field the app's projection does not read rather than evidence nobody has. Accounts still differ from
each other. This knowingly deviates from the [egress specification](../egress/SPEC.md)'s "bind only
an account dedicated to one workload".

**The workload must never read the token.** `automountServiceAccountToken: false`, with the
projected audience-scoped token mounted by the egress sidecar alone, is what keeps a shared account
out of the container an agent runs commands in. Every template this surface stamps holds that
invariant; breaking it costs the whole account's authority rather than one box's.

## Separation from the integration app

**Its own label and selector.** Not `agentplane.allegedly.works/managed`, which the app lists,
watches and gates operations on (<../app/inventory.py>, <../app/live.py>). An exec target carrying
it joins the app's fleet view and its ingestion coordinator, which then discovers a runner the box
does not have.

**Its own template set**: a caller names a SandboxTemplate, but only one the group's reviewed
configuration offers, at the same operator cadence as `action_groups`. A free-form choice would let
a caller stamp any template in the namespace. Staging offers its own `agentplane-sandbox` template
on the plain sandbox image (<../../cluster/cdk8s/agentplane/command_sandbox.py>), a build-sized
box, and the app's runner template, and a caller always names one. Each template describes itself:
its `sandbox-actions.agentplane.allegedly.works/description` annotation is what `create` tells an
agent, read once when the service starts, and a command runs in the container `kubectl exec` would
pick, the Pod template's `kubectl.kubernetes.io/default-container` or else its first.
`get_template` returns one offered template whole, read when asked and less only
`metadata.managedFields`. **Every caller can read everything in an offered template**, so a template
references Secrets and never inlines one.

**Its own inventory code**, reusing the CRD shapes rather than `SandboxInventory`, which hardcodes
the per-sandbox account, the app's label and the app's ownership model. The CRD vocabulary both
share — API coordinates, the `agents.x-k8s.io/pod-name` annotation, condition lookup — is
<../../util/agent_sandbox.py>.

## Kubernetes from inside the box

The box reaches the API server the way it reaches everything else: a placeholder in the request, an
`EgressCredential` the central proxy substitutes, and a rule admitting `kubernetes.default.svc`.
The workload holds no Kubernetes credential, exactly as it holds no Forgejo one.

The audience is the part that needed its own mechanism. The hop bearer is projected with the
proxy's own audience (`agentplane-egress`), and the API server validates against its
`--api-audiences`, so that token is refused there. The sidecar therefore projects a _second_ token
carrying the API server's audience, mounted by the sidecar alone and rotated by kubelet, presented
on the hop for the proxy to substitute into `Authorization` on the rules that name it — the
`projectedWorkloadToken` credential source (<../egress/SPEC.md>). The proxy gains no Kubernetes
rights, the workload sees neither token, and the container boundary holding it is the one the design
already rests on ([identity evidence](sandbox_egress_identity_evidence.md)). Before substituting,
the proxy reviews that token against the API server audience and requires it to resolve to the same
Pod it authenticated, so a substituted credential provably belongs to the caller being decided.

RBAC is then standing on the calling account, beside its `EgressBinding`s, and every sandbox of that
account has it. Under the contract above that is the intent rather than a leak.

Verified from a sandbox on staging, 2026-09-19: a `SelfSubjectReview` through the proxy returns
`system:serviceaccount:agentplane-staging:claude-ai` with
`authentication.kubernetes.io/pod-name` and `pod-uid` naming the calling box. The identity the API
server sees is the caller's account, bound to the Pod that asked.

**Gotcha: the rule admits a host by name, not by address.** `kubernetes.default.svc.cluster.local`
is what the rule names, so `https://kubernetes.default.svc` — the same Service, the same address —
is denied `no-rule`. Use the fully qualified name.

**Not exercised: the upgrade verbs.** `exec`, `attach` and `port-forward` negotiate SPDY or
WebSocket and a watch streams chunked, none of which has been run through the bumping proxy. Haku's
own API proxy answers `501` to the upgrade verbs, so that is where to expect trouble.

## Tool surface

`create`, `exec` and `list`, with `info`, `get_template` and `dispose`. `create` returns once the
object exists and the caller polls `info` for the controller's conditions.

**Conditions are passed through as the controller wrote them**, not summarised into a state. A
reading taken here would be a second opinion that can disagree with the authority and carries less
than it did: `ReconcilerError` with an exceeded-quota message, `DependenciesNotReady` with
`Pod exists with phase: Pending`, and `Suspended` are three different answers to "why is this box
not ready", and only the controller's own `reason` distinguishes them.

**`create` does not wait for readiness.** A box can stay unready indefinitely -- refused by the
namespace quota, or stopped on another `ReconcilerError` -- so a waiting `create` would spend its
whole timeout to report a deadline, where `info` reports the controller's own reason for it.

**Gotcha: a box the quota refused does not start when the quota frees.** agent-sandbox (v0.5.5)
returns the Pod-create error from its reconcile, so controller-runtime retries that Sandbox on its
per-object error backoff, doubling to a 1000 s cap; nothing watches the `ResourceQuota`. A box that
has been failing for a while therefore waits up to about 17 minutes after the quota frees. On
staging, a refused box kept the same condition for as long as it was polled after the quota freed,
while `dispose` + `create` of it reached `Ready` in about 45 s. So an exceeded-quota
`ReconcilerError` is the caller's cue to `list` its sandboxes, `dispose` the ones it has finished
with, then `dispose` + `create` the refused one.

Shapes follow <../../haku/console/tools/sandbox.py>, the surface already in daily use: one
bounded Bash script per call, a per-environment ceiling on timeout and retained output patched into
the advertised schema, and a nonzero exit reported as a result rather than a transport error.

Properties of the Action path the tool documentation has to state, because an agent assuming
otherwise misreads every call:

- submission is non-blocking and a receipt is not execution success; the caller polls the durable
  event sequence to a terminal state;
- nothing is retried after dispatch, which is what a command with side effects needs, and
  `execution_unknown` is reported as itself rather than as a failure;
- an `ActionPolicySet` and a binding for the calling account are the access control. Without them
  every command waits for a human, which is not an exec loop.

## Rejected alternatives

**An MCP backend carrying the caller in per-call metadata.** The pinned FastMCP has the seam —
`Client.call_tool(meta=...)` and server-side `Context.request_context.meta` — and it is the only
per-call one. What killed it is cost, not mechanism: it introduces a caller assertion on the shared
authorization path, which then has to be defended permanently — a tool argument named `caller` must
not forge it, envelope `origin`/`correlation` must never become it, and groups that did not ask for
it must not receive it. `request.caller` needs none of that. Such a backend would also not have
avoided the privilege: it cannot authenticate the caller itself, so it can only trust the Action
Service's static bearer, and the bearer is then the capability one hop away. The split buys process
isolation, not privilege isolation.

**Forwarding the caller's own credential.** There is no caller credential at dispatch. Submission
and dispatch are separated by a human Decision, a possible restart, and a possibly different
replica, so a Pod-bound token or an OAuth access token has expired or is simply absent.
`ExternalGrantProvenance` holds issuer, client, connection, grant and revision, and no token, by
design. Making one exist would mean retaining a credential in the request, which the service
refuses, or minting one per caller through TokenRequest, which would let the Action Service act as
any account that can call it.

**Standing bindings on the caller, mirrored onto per-sandbox accounts.** The alternative that keeps
per-sandbox accounts: read the caller's `EgressBinding`s and write derived ones onto each new
account. It breaks a property the egress specification states plainly — "creating one is the whole
act of allowing, and deleting it the whole act of taking that back" — because deleting the source
takes back nothing already copied. Kubernetes cannot repair that: garbage collection deletes a
dependent when its _last_ owner goes, and revocation needs the _first_. Renewal by the provisioning
component is already rejected in [egress composition](egress_composition.md) for putting that
component back in the enforcement path. What is left is a bounded expiry on every derived binding,
and revocation complete only within that window.

**The proxy mints a Kubernetes token per request.** `TokenRequest` for the account that just
authenticated, bound to the Pod its review named. It needs `create` on `serviceaccounts/token` in
every namespace the proxy accepts bearers from, on the one component already holding every
substituted credential — the grant shape `EGRESS_SOURCE_ADDRESS` removed the proxy's `pods` read to
avoid.

**Mounting the Kubernetes token in the workload container.** The simplest source, and it gives up
what the placeholder design exists for: a credential inside the box can be copied out and used
elsewhere until it expires, where substitution confines the identity to requests the proxy admitted.
"A shell as yourself" bounds authority inside the box; it does not extend to a portable credential
for that account. It also breaks the sidecar-only projection above.
