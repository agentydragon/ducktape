# Sandbox Actions: exec targets that run as the caller

An Action group that creates a box, a plain Pod running as its caller, and runs bounded commands in
it. It exists for callers outside the cluster — Claude Code web and claude.ai, bound by OAuth to a
ServiceAccount — which have no Pod and so no other way to reach an in-cluster box, and for agents in
Session Sandboxes that want a box of their own.

A box is not a Session Sandbox ([ADR](../../docs/adr_action_sandboxes.md)). The Sandbox Service
provisions a Sandbox to _host an agent_: a runner, a state volume, an account of its own. This
surface creates a box to _run commands in_, driven by an agent that lives elsewhere. They share the
Pod recipe (`cluster/cdk8s/agentplane/sandbox_pod.py`) and nothing else.

## The contract

**A sandbox is a shell as yourself.** It runs as the calling ServiceAccount, reaches what that
account's `PolicyBinding`s allow, and — because a Pod proves its ServiceAccount, and the account
carries `agentplane.allegedly.works/use-action-service` — is admitted to the Action Service as that
same account. No caller obtains authority it did not already hold.

That answers the question an operator actually has when asked to bind this Action for a new
ServiceAccount: it is exactly as safe as that account's own authority, and granting it grants
arbitrary code execution under that identity.

The guard the rule rests on: the ServiceAccount comes from `ExecutionRequest.caller`, which the
authenticated admission path sets. It is never a tool argument. A surface accepting one would mean
"stamp a Pod as any account you can name".

The Action Service's own ServiceAccount creates, reads and deletes Pods and holds `pods/exec` in the
sandbox namespace, and exec runs in the Action Service process. `timeout_seconds` and `max_output_bytes`
ceilings keep a long build from streaming unbounded output into that Pod, and the command's own
ceiling is the only bound on how long it runs: the executor renews the execution lease underneath
it, so a call lasting minutes is ordinary rather than an attempt the sweep reclaims.

## One ServiceAccount per caller, not per sandbox

Every sandbox this surface stamps runs as the caller's account. The Sandbox Service does the
opposite — an account per Sandbox, owner-referenced to it — and the difference is deliberate, with
intended consequences.

**A sandbox is a caller.** It can create and exec sandboxes itself, including ones it did not
create, because they are one principal. The operation is closed: no chain gains authority.

**Egress attribution collapses per principal in the app's view, not in the record.**
`DecisionsClient.recent` (<../../egress/decisions_client.py>) asks the proxy for decisions _by ServiceAccount_
and projects a `Decision` with no Pod field, so `GET /sandboxes/{name}/egress/decisions` returns one
combined stream for every sandbox of one account. The proxy's own record does carry
`source_pod_uid`, persisted with the rest (<../../egress/decisions.py>), so per-box attribution is a
field the app's projection does not read rather than evidence nobody has. Accounts still differ from
each other. This knowingly deviates from the [egress specification](../../egress/SPEC.md)'s "bind only
an account dedicated to one workload".

**The workload must never read the token.** `automountServiceAccountToken: false`, with the
projected audience-scoped token mounted by the egress sidecar alone, is what keeps a shared account
out of the container an agent runs commands in. Every template this surface stamps holds that
invariant; breaking it costs the whole account's authority rather than one box's.

## Separation from the integration app

**Its own label and selector.** Not `agentplane.allegedly.works/managed`, which the Sandbox Service
lists, watches and gates operations on (<../../sandboxes/inventory.py>). A box carrying it
would join the fleet view, which then looks for a runner the box does not have.

**Owned by its caller's account.** Each box carries an owner reference to the ServiceAccount it runs
as. An agent in a Session Sandbox calls this surface as that Sandbox's own account, so the boxes it
creates are garbage-collected with the Sandbox; a static account's boxes last until `dispose`.

**Its own template set**: a caller names a template, but only one the group's reviewed
configuration offers, at the same operator cadence as `action_groups`, with the description an agent
chooses it by. Each names a core `PodTemplate` in the namespace that cdk8s renders from the shared
recipe (<../../../cluster/cdk8s/agentplane/command_sandbox.py>): an object rather than a Pod spec in
the configuration, so the image pins rewrite its containers. Staging offers a command box on the
plain sandbox image and a build-sized box. The Action Service refuses to start on an offered template
that is missing, lacks the egress sidecar or lets the workload mount a ServiceAccount token, and
checks again at each `create`. A command runs in the container `kubectl exec` would pick, the Pod's
`kubectl.kubernetes.io/default-container` or else its first. `get_template` returns one offered
template's Pod whole. **Every caller can read everything in an offered template**, so a template
references Secrets and never inlines one.

## Kubernetes from inside the box

The box reaches the API server the way it reaches everything else: a placeholder in the request, an
`EgressCredential` the central proxy substitutes, and a rule admitting `kubernetes.default.svc`.
The workload holds no Kubernetes credential, exactly as it holds no Forgejo one.

The audience is the part that needed its own mechanism. The hop bearer is projected with the
proxy's own audience (`agentplane-egress`), and the API server validates against its
`--api-audiences`, so that token is refused there. The sidecar therefore projects a _second_ token
carrying the API server's audience, mounted by the sidecar alone and rotated by kubelet, presented
on the hop for the proxy to substitute into `Authorization` on the rules that name it — the
`projectedWorkloadToken` credential source (<../../egress/SPEC.md>). The proxy gains no Kubernetes
rights, the workload sees neither token, and the container boundary holding it is the one the design
already rests on ([identity evidence](../../docs/sandbox_egress_identity_evidence.md)). Before substituting,
the proxy reviews that token against the API server audience and requires it to resolve to the same
Pod it authenticated, so a substituted credential provably belongs to the caller being decided.

RBAC is then standing on the calling account, beside its `PolicyBinding`s, and every sandbox of that
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
replica, so a Pod-bound token or an OAuth access token has expired or is simply absent.
`ExternalGrantProvenance` holds issuer, client, connection, grant and revision, and no token, by
design. Making one exist would mean retaining a credential in the request, which the service
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
