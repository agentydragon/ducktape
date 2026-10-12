# ADR: Action Service boxes are not Sandbox Service Sandboxes

- **Status:** Proposed (accepted in discussion with the operator on 2026-10-12)
- **Date:** 2026-10-12
- **Scope:** the `sandbox__*` Actions ([`action_service/sandbox/`](../action_service/sandbox/README.md)),
  the [Sandbox Service API](../sandbox_service/API.md) and the templates both stamp from

## Context

Two components stamp agent-sandbox `Sandbox` objects: the Sandbox Service, for the Sandboxes that
host Sessions, and the Action Service, whose `sandbox__*` Actions give a caller a shell. They share
the CRD, the namespace and its quota, and the Pod shape in
[`cluster/cdk8s/agentplane/sandbox_pod.py`](../../cluster/cdk8s/agentplane/sandbox_pod.py) (egress
sidecar, tokens only the sidecar mounts, the interception CA, the proxy environment). Staging even
offers the runner templates to the Action Service.

The Sandbox that hosts a Session is heading to an Agentplane-owned resource: a state PVC that outlives
each Pod or VM, a runner on the control channel, its own ServiceAccount with policy bindings owned by
the Sandbox, pause, image swap and resume, and a seal before delete. An Action Service box has none of
that:

|           | Session Sandbox (Sandbox Service)                       | Action Service box                           |
| --------- | ------------------------------------------------------- | -------------------------------------------- |
| Runs as   | a ServiceAccount minted per Sandbox, owned by it        | the caller's own static ServiceAccount       |
| Grants    | policy bindings owned by the Sandbox                    | the caller's standing bindings; none created |
| Runner    | dials the Runner Gateway and the History ingress        | none; commands arrive by `pods/exec`         |
| State     | a PVC that outlives every incarnation                   | none beyond the Pod                          |
| Lifecycle | pause, image swap, resume; seal every Session to delete | create, exec, delete                         |
| Backends  | Pod or KubeVirt VM                                      | Pod                                          |

## Decision

### 1. Two kinds of box, one shared Pod recipe

They are different things, not one resource with two shapes. The Sandbox Service stays the
provisioner of Session Sandboxes only: its `CreateSandbox` gains no caller-workload variant, no
forwarded `run_as` identity and no exec RPC. The Action Service keeps provisioning its own boxes,
as **plain Pods** rather than agent-sandbox `Sandbox` objects, so Agentplane can stop using
agent-sandbox once Session Sandboxes move to their own resource. Action boxes lose agent-sandbox's
suspend; `dispose` and an idle expiry bound their lifetime.

What they share is the Pod recipe, a cdk8s library (`sandbox_pod.py` today), not a service API.

Rejected: **one `CreateSandbox` with an agent and a caller workload**, the first draft of this ADR.
Every feature the Session Sandbox is gaining (state PVC, runner and control channel, owned bindings,
pause and image swap, VM backend) is one a caller box must refuse, and the Sandbox Service would gain
a forwarded identity and `pods/exec` to serve boxes that use none of its machinery.

### 2. Two template catalogs; no template is valid for both

- **Session Sandbox templates** are an Agentplane template kind beside the Sandbox resource, read only
  by the Sandbox Service: backend (`pod` or `vm`), runner image and harnesses, state volume size and
  mount path, and the Pod (or VM) shape. The Sandbox Service fills in the per-Sandbox
  ServiceAccount, the control and ingress targets and the state PVC; a template names none of them.
- **Action Service box templates** are entries in the Action Service's reviewed configuration: a
  name and the description an agent chooses by, each naming a core `PodTemplate` cdk8s renders
  beside it with `automountServiceAccountToken: false`. The Action Service fills in the Pod name,
  its labels, its owner and `serviceAccountName: <caller>`. A CiliumNetworkPolicy on the box label
  is the fence: DNS and the egress proxy out, nothing in. Rejected: **the Pod spec inline in the
  configuration.** The image pins (`image-pins/kustomization.yaml`) rewrite containers in any
  object but never inside a ConfigMap string, so every box would run the `unset` placeholder tag.

Each service reads only its own catalog, so "which templates may this caller choose" needs no flag.
A box that wants the ducktape toolchain gets its own Action template on that image, not the runner
template.

### 3. The egress path is in the template; each service checks it

cdk8s builds the egress sidecar into every template through the shared recipe, so the reviewed
template is the whole Pod and the sidecar's image pin stays with the other image pins. VM Sandboxes
get their relay from the admission policy on virt-launcher, not from a Pod spec, so injection at
runtime would cover only pods anyway. Each service refuses at startup a template that lacks the
sidecar or mounts the workload's ServiceAccount token. A box without the sidecar would already fail
closed (its network policy allows only DNS and the proxy); the check makes that a configuration error
instead of a box with no network.

Rejected: **the service injects the sidecar at create.** It moves the sidecar's configuration into
two services and makes the reviewed template differ from what runs.

### 4. Action boxes run beside Session Sandboxes

An agent in a Session Sandbox can create an Action box, and the box runs as that Sandbox's own
ServiceAccount: the Sandbox Service labels every account it mints for the Action Service, so the
agent's call arrives as that account and the box gets exactly the agent's egress and Action
authority. A Pod runs only as a ServiceAccount in its own namespace, and those accounts live in the
Session Sandboxes' namespace, so Action boxes do too, and the Action Service keeps `pods/exec` there.
Kubernetes RBAC cannot scope `pods/exec` by label, so that exec also reaches Session Sandboxes.

Each box carries an owner reference to its caller's ServiceAccount, so deleting a Session Sandbox
(and with it the account it minted) garbage-collects the boxes its agent created. Boxes of static
accounts such as claude-ai are unaffected; `dispose` and an idle expiry bound them.

Rejected: **a namespace of their own.** It would scope the Action Service's Pod authority to Action
boxes, but every caller account, including each per-Sandbox account the Sandbox Service mints, would
need a counterpart there with its bindings.

## Consequences

- The Sandbox Service's API and authority stay as they are; the Action Service is not its client.
- The Action Service swaps its agent-sandbox inventory for plain Pods and its `SandboxTemplate`
  allowlist for its own `PodTemplate`s; staging stops offering it the runner templates.
- Retiring agent-sandbox waits on the Session Sandbox resource and on Action boxes becoming plain
  Pods, and on nothing else.
