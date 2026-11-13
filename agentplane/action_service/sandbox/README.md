# Sandbox Actions: exec targets for an Identity

An Action group that creates a box and runs bounded commands in it. A box is created for the
reviewed Identity effective caller on the request, whether the actor is an agent in a Session Sandbox
or an external Connection. An actor's platform ServiceAccount authenticates the service call; it does
not become the box's workload subject. A platform actor needs reviewed delegation to name an Identity.

A box is not a Session Sandbox ([ADR](../../docs/adr_action_sandboxes.md)). The app requests a
Session Sandbox to _host an agent_: a runner and a state volume. This surface creates a box to _run
commands in_, driven by an agent that lives elsewhere. They share the Pod recipe
(`cluster/cdk8s/agentplane/sandbox_pod.py`) and nothing else.

## The contract

**A box has the Identity's egress authority.** The authenticated admission path sets
`ExecutionRequest.caller` only after reviewing delegation; tool arguments cannot choose it. The
service requests a `WorkloadCredential` for that Identity, pinned to its UID and bound to the box's
Pod ([the record contract](../../docs/workload_authentication.md#workloadcredentials)). The issued
SVID is mounted into the egress sidecar alone, which presents it to the proxy. The proxy applies the
Identity's egress rules. Workload containers receive only the local proxy address and public trust
bundle; they mount neither a token nor an SVID.

The Pod uses the namespace's `default` ServiceAccount with token automount disabled. Kubernetes
access from a box is granted by an explicit egress rule and `kubernetesServiceAccount` credential;
the proxy uses its own API token to impersonate the named account. Neither the box's ServiceAccount
nor the actor's platform ServiceAccount is inferred as the workload's Kubernetes principal.

The Action Service's own ServiceAccount creates, reads and deletes Pods and holds `pods/exec` in the
sandbox namespace, and exec runs in the Action Service process. `timeout_seconds` and `max_output_bytes`
ceilings keep a long build from streaming unbounded output into that Pod, and the command's own
ceiling is the only bound on how long it runs: the executor renews the execution lease underneath
it, so a call lasting minutes is ordinary rather than an attempt the sweep reclaims.

## Separation from the integration app

**Its own label and selector.** Not `agentplane.allegedly.works/managed`, which identifies Session
Sandboxes that carry runners (<../../runner_gateway/environments/internal/management/access.go>). A
box carrying it would join the fleet view, which then looks for a runner the box does not have.

**Owned through its credential.** Each box's Pod is owned by its `WorkloadCredential`. The record is
unowned and names the exact Identity incarnation: acting as an Identity confers no authority over a
box, so boxes last until `dispose`, and deleting the Identity only revokes their credentials. The
caller labels, including the Identity UID, keep records and boxes from following a same-named
successor; a new Identity never adopts them. Retained ServiceAccount-keyed boxes and records are
left intact and are not adopted by matching names. The record precedes its Pod because the Pod names
the record's Secret when it is created, so the Pod cannot own the record.

**Its own template set.** A caller names a template, but only one the group's reviewed configuration
offers, at the same operator cadence as `action_groups`, with the description an agent chooses it
by. Each names a core `PodTemplate` in the namespace that cdk8s renders from the shared recipe
(`<../../../cluster/cdk8s/agentplane/command_sandbox.py>`): an object rather than a Pod spec in the
configuration, so the image pins rewrite its containers. Staging offers a command box on the plain
sandbox image and a build-sized box. The Action Service refuses to start on an offered template that
is missing, automounts a ServiceAccount token, or projects one, and checks again at each `create`. A
command runs in the container `kubectl exec` would pick, the Pod's
`kubectl.kubernetes.io/default-container` or else its first. `get_template` returns one offered
template's Pod whole. **Every caller can read everything in an offered template**, so a template
references Secrets and never inlines one.

## Kubernetes from inside the box

Commands use the Kubernetes credential's host, placeholder and invocation from the rules response.
The proxy substitutes its own bearer for the explicitly named ServiceAccount; no kubeconfig or
Kubernetes token is mounted in the workload. Kubernetes RBAC applies to that named account, while
the egress rule granting its use is bound to the box's Identity.

## Tool surface

`create`, `exec` and `list`, with `get`, `get_template` and `dispose`. `create` returns once the Pod
exists rather than once it is ready: an image pull or a full node can hold a box unready for longer
than a call should wait, where `get` reports why. A full namespace quota refuses `create` outright;
an evicted box stays `Failed`, so `dispose` plus `create` is how a caller gets it back.

`exec` takes one bounded Bash script per call: the deployment's ceiling on timeout and retained
output is patched into the advertised schema, and a nonzero exit is a result rather than a transport
error. Submission is non-blocking, dispatch does not retry a command with side effects, and an
`execution_unknown` outcome remains unknown rather than being reported as a failure.

A `PolicySet` and `PolicyBinding` for the effective Identity authorize its Actions. Without them,
every command waits for a human, which is not an exec loop. Staging also offers these as direct tools
to external Connections (`sandbox__exec` and related actions); a call answers with its result, or
with its request after 30 seconds for `get_action_result` to wait on. A command that outlives the wait
keeps running; returning the request rather than holding the call open keeps a client that gives up
first from running the script again.

## Rejected alternatives

**Forwarding the actor's ServiceAccount credential.** The platform actor authenticates the service
call, but that authentication is not the box's workload subject. Submission and dispatch are
separated by a human Decision, a possible restart, and a possibly different replica; retaining a
token in the request would turn a platform credential into transferable workload authority. A
reviewed Identity delegation selects the effective caller instead.

**Mounting a Kubernetes token in the workload container.** A credential inside the box could be
copied out and used elsewhere, outside the egress policy that governs the box. Keeping API access
behind the proxy confines the named account to requests the Identity's rules admit.
