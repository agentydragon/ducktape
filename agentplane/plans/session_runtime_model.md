# Sessions, harness placement and execution attachments

Operator-requested future design, 2026-10-28 PDT. No concrete domain model or implementation is selected.
Prefer the harness outside the execution environment when capabilities are otherwise equal: agent
commands then have less opportunity to interfere with harness code/state, tool authority can be
explicitly mediated, and colocation does not become an unavoidable lifetime constraint. The boundary
still depends on actual tool routing and access; placement alone does not establish isolation.

Agentplane has largely represented a harness and its Session state as living inside a Sandbox.
The experimental [central sampler](../x/central_sampler/README.md) hosts them outside execution
Sandboxes; its Session data should survive their deletion. In that mode a Sandbox supplies tools,
rather than defining the conversation's lifetime. It still requires a running executor for the first
open and fixes the binding to one Sandbox incarnation; those are v1 constraints, not a required
ontology for every future harness.

## Cases the model must represent

- A Sandbox exists first, and an official Claude Code or Codex CLI runs there with its native
  state. Keeping the ability to wrap third-party harnesses is independently useful.
- A central Session exists first and can run without a Sandbox, using external MCP tools only.
- A central Session attaches an execution environment later, detaches it, or loses it through
  deletion, while retaining conversation identity and continuation data.
- A harness's capabilities constrain which relationships and lifecycle changes it supports.
  Claude Code naturally runs in the environment it acts in. Disabling built-in tools or adding
  remote tools may adapt it, but does not make independent execution placement its native contract.

Neither Sandbox-before-Session nor Session-before-Sandbox is a universal requirement. Independent
placement should be representable without requiring it of every backend. Preserve meaningful
backend differences rather than exposing operations a harness cannot perform.

## Partial remote support is not a placement contract

The operator reports that few harnesses offer complete separation today, citing OpenClaw's
historical support: some features execute remotely while many still assume the gateway's local
filesystem/process environment.

The dated [OpenClaw execution-model record](../../cluster/docs/operations/2026_02_24_openclaw_execution_model.md)
provides a concrete example: node mode forwarded shell commands while built-in file tools stayed
on the gateway; the integration shared a workspace volume to keep them consistent. Docker sandbox
mode had different routing, so this is evidence about that integration, not every OpenClaw mode
or release. A shared mount can restore consistency while weakening the intended state boundary.

Assess each backend across shell, filesystem/patch/image tools, instruction discovery, skills and
MCP, hooks, cwd/path resolution, persistence and recovery. Record unsupported or gateway-local paths
explicitly. Do not label a backend fully remote merely because it forwards shell commands, or force
an incomplete adapter to impersonate a capability-complete external harness. Preserve colocated
CLI operation when that is the supported way to retain its useful behavior.

## Questions for review

Consider distinguishing Session identity and continuation authority, the harness runtime and its
placement, and execution-environment attachments. This is a candidate vocabulary, not a decision
on object types, services, databases or Kubernetes resources.

Decide which lifetimes are coupled for each backend; whether attachments may be absent or multiple;
and how creation, attach/detach, suspend, deletion and recovery appear in the API and UI. Separate
retained history from continuation needed to resume a harness, and state what each backend can keep
or recover after its environment disappears. Preserve existing Session identities and native state.

Define attachment authorization and capability discovery, incarnation fencing, in-flight tool
cancellation and uncertain effects. Attribute sampling to the Session and execution to the actual
environment used. Specify how cwd, filesystem instructions, credentials and tool availability
change with attachments; detachment must not silently redirect commands to the sampler host.

The versioned [Codex environment API evidence](sampling_outside_sandboxes.md#codex-supports-the-split-upstream)
already describes experimental `environment/add`, per-thread environments and turn overrides.
Use it as input; it does not establish arbitrary detach/rebind or environment-free operation.

Exit: reviewed terms, ownership/lifetime rules, API examples for the cases above, and a
transition proposal preserving existing identities, history and native state. Implementation work
follows a reviewed choice. This design authorizes no deployment or migration.
