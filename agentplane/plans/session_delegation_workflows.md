# Caller-privilege Session delegation

Status: **existing-environment delegation implemented; natural-agent usability is an operator
follow-up; fresh provisioning deferred.** The workflow capstone is tracked under
[`THREAD_CREATE_AUTHORIZATION`](task_dag.md#thread_create_authorization--caller-privilege-delegation-capstone).
The implemented surface is the [`session_delegation` Action group](../action_service/session_delegation/README.md)
(`environments__{list,get,resume}` and seven `sessions__*` tools), the [Session CLI](../docs/agent_cli.md)
and the maintained [Session delegation guide](../action_service/docs/agents/session_delegation.md),
over [Dispatcher authorization](../runner_gateway/dispatcher/SPEC.md), [retained History
access](../history_service/README.md) and the [delegated authorization contract](../docs/delegated_authorization.md).
Scripted acceptance is `//agentplane/system_tests/suites/mcp:test_delegation` (external MCP, real
Connection consent, integration app scaled to zero) and
`//agentplane/system_tests/suites/mcp:test_parent_delegation` (parent CLI). Both prescribe model
calls; they do not establish unaided natural-agent usability.

## Remaining usability capstone

**Evaluate natural delegation through normal use.** Give a real agent an ordinary assignment and
verify it discovers the target and schemas, prepares its directory, opens a child, submits separate
input and reads a correlated result. Include follow-up and control/recovery when needed. Cover an
existing-environment parent and an external MCP caller without private fixture handles or a prescribed
tool script. The operator will test with a prompt later; this is not a merge or rollout gate
(operator, 2026-11-02 UTC). Replan concrete backend gaps if discovered.

Reuse the scripted suites and owning service tests for published-schema behavior, separate opening
and first input, command correlation and app-independent operation. Preserve authorization and
recovery requirements: foreign-SA denial, forged parent/principal, Session/command mismatch,
account/target replacement, read revocation, History lag/gaps, concurrent inputs and supported
retry/reconnect. Address concrete gaps in that coverage; the new usability exercise need not
repeat the entire matrix or the operator-waived sampler disruption drill.

An empty target list must clearly require an operator-prepared environment today. Exercise the
request/approval/creation workflow when future provisioning ships; do not invent a successful
launch before it exists. Owning the JS host, the full runtime model and native-child projection
remain unnecessary for the current MCP/CLI workflows. Central code-mode coordinators invoke the
Agentplane CLI through `exec`; they have no `sessions` JavaScript global.

Owning a JS host may later allow ordinary callable libraries, serializable environment handles,
cancellation and native errors such as `error.observed_output.text`. The pinned host's current
string-rejection adapter is not that API; the separate host decision evaluates those ergonomics.

A child saying it is blocked is model-authored output; the first slice manufactures no backend
blocked-task state. Select a typed `report`/`blocked` tool separately if that proves insufficient.

## Instruction composition still open

The shared [Session instructions composer](../session_instructions/README.md) adds platform guidance,
the Session ID and the caller's additive `additional_instructions`
([Runtime](../env/runtime/docs/session_protocol.md), [central sampler](../central_sampler/SPEC.md)).
Not yet decided or built:

- **Environment context:** the selected target's description and instructions as a backend-added
  layer. It describes the environment and establishes no execution permission.
- **Role/preset instructions:** the first tools accept no preset names and have no backend preset
  registry; a client combines its concrete role text into `additional_instructions`. Generic
  Agentplane guidance currently bundled with a preset belongs in the platform layer.
- **Platform guidance override:** deliberately omitting or replacing platform guidance needs its own
  reviewed advanced option, never an empty `instructions` value or ordinary additional briefing.
  Prompts are not an authorization boundary.
- **Ownership:** which component owns guidance configuration and composition is a
  [later design](task_dag.md#platform_guidance_ownership_design--decide-platform-guidance-ownership-and-composition).

## Budget and broader-policy boundaries

Count limits do not replace spend controls: the operator's stated concern is provider usage cost.
A future per-account spend budget belongs at LLM ingress and also covers message-triggered work.
No new spend-budget implementation or general different-privilege delegation policy gates this
caller-privilege slice. Broader launch/default/grant and cross-SA archive decisions remain in
their existing DAG nodes, and existing `orchestration_v1` broker grants remain explicit until
[`ORCHESTRATION_V1_BACKEND_GRANTS`](task_dag.md#orchestration_v1_backend_grants--orchestration_v1-operations-on-backend-grants)
has a reviewed migration. Avoid a second general authorization owner in a new facade.

## Deferred: provisioning when no eligible environment exists

Deferred at the stakeholder's request until template catalogue ownership/location is settled, under
[`ENVIRONMENT_MANAGEMENT`](task_dag.md#environment_management--dedicated-environment-tools-and-creation).
Today Pod/VM workload templates are namespaced `SandboxTemplate` objects generated from Git-managed
cdk8s configuration; launch defaults also live in app/deployment presets. `EnvironmentService`
already has `ListEnvironmentTemplates` (runtime template names) and typed `CreateEnvironment`.

### Accepted choices, 2026-10-31

- **Full template catalogue:** return the complete deployment-published catalogue, with pagination
  and no per-SA template filtering. Publication means offered for submission, not approved. Any
  future per-template restriction needs an explicit policy owner, never inferred from catalogue
  membership or a preset. Provide its backend read and CLI without an app dependency.
- **Creation identity:** reuse the caller's existing SA and live permissions without owning or
  changing that SA, adding PolicyBindings or manufacturing grants. Request extra access separately.
- **Template lifetime:** pin the approved configuration across restarts; updating it is an explicit
  operation. Mutable templates currently resolve again on every start, so a revision/snapshot
  contract is new controller/provisioner work, as is controller support for a borrowed account with
  pinned UID.
- **Approval:** creation is a dedicated `environments/create` Action with human approval initially,
  with no `orchestration_v1` dependency. Its request shows the concrete template/backend, resources,
  execution account and workload mounts/security settings; approval covers those parameters, not
  arbitrary future Session control or a different account. No blanket auto-approval rule for
  environment management. Other lifecycle Actions (stop, delete, resize, edit) need their own
  reviewed scope and must account for every Session sharing the environment.

The existing [`orchestration_v1` broker](../action_service/orchestration_v1/README.md) creates a new
SA and remains a separate legacy capability; its `launcher_drives_only_the_sandbox_it_launched`
scenario proves that brokered flow, not this interface.

### Sketch: workflow

1. Discover `environments__list_templates` through `tools/list` and call `{"limit":20}`. Retain the
   complete `template` reference, including its revision. Discover `environments/create` and its
   schema/policy through the normal Action catalog; do not guess template names, revisions or
   resource defaults.
2. Retain a creation UUID and a separate Action key, then submit through `request_action` (direct
   Action tools only execute auto-approved calls):

   ```json
   {
     "request": {
       "idempotency_key": "ci-vm-creation-2026-10-31-01",
       "action": { "group": "environments", "name": "create" },
       "arguments": {
         "creation_id": "0698a0d2-fb0d-4c84-a17e-e7c85f5c57a4",
         "name": "ci-worker-vm",
         "template": {
           "namespace": "agentplane",
           "name": "ci-vm",
           "uid": "4ab7e198-02c4-472f-8b5d-61d533760229",
           "revision": "opaque-discovered-configuration-revision"
         }
       },
       "title": "Create the reviewed CI VM environment"
     },
     "wait": { "wait_seconds": 30, "wait_until": "terminal" },
     "respond_with": "result"
   }
   ```

3. Pending approval, denial, allowed Decision and Execution outcome are separate Action facts. After
   a lost reply or expired wait, use `get_action_request` with the retained key; never resubmit under
   a new key.
4. Call `environments__get_creation` with the `creation_id` to observe the backend independently of
   Action polling.
5. Refresh the created target with `environments__get` and continue with the existing
   create → submit → observe workflow. An approved creation does not create a Session, guarantee
   readiness or grant a different principal authority.

### Sketch: tools and records

| Tool / Action                  | Purpose                                                                                                       | Approval / authorization                                                                                            | Input                                                                                         | Output                                                    |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `environments__list_templates` | Discover the full published Pod/VM catalogue, exact revision handles and what a creation approval covers.     | Authenticated read of the full catalogue; discovery is not creation approval.                                       | `{limit: integer (1..100), after?: OpaqueCursor}`                                             | `Page<TemplateDescriptor>`                                |
| `environments__get_creation`   | Look up a creation by retained ID after a lost reply; neither repeats creation nor approves a pending Action. | Automatic for the creation's authenticated owner.                                                                   | `{creation_id: UUID}`                                                                         | `CreationObservation`                                     |
| Action `environments/create`   | Create a managed environment under the caller's account from the reviewed revision; returns a receipt.        | Human approval initially, plus backend validation of the actual caller/account, namespace, revision, configuration. | `{creation_id: UUID, name: string (Kubernetes DNS subdomain, 1..253), template: TemplateRef}` | `CreationObservation` inside the ordinary Action envelope |

```typescript
// EnvironmentDescriptor is the existing EnvironmentService message; AccountRef is namespace/name/uid;
// ToolError is a structured error with code, source and effect (not_started, unknown, not_applicable).
type TemplateRef = { namespace: string; name: string; uid: OpaqueId; revision: OpaqueId };
type TemplateDescriptor = {
  template: TemplateRef;
  backend: "pod" | "vm";
  configuration: {
    workload: SandboxTemplateSpec; // existing public Pod/VM and volume schema
    execution_account: AccountRef; // resolved with the verified caller's account
    bootstrap?: string;
    wake_on_send: boolean;
    idle_suspend_after_seconds?: integer; // >= 60; absent means no idle suspension
  };
};
type CreationObservation = { creation_id: UUID; source: "environment" } & (
  | { kind: "not_observed" }
  | { kind: "provisioning" }
  | { kind: "created"; environment: EnvironmentDescriptor }
  | { kind: "failed" | "unknown"; error: ToolError }
);
```

`TemplateDescriptor` projects published `SandboxTemplate` configuration and launch defaults; it is
not a new persisted entity. Its revision covers the complete resolved configuration, including the
execution account. The creation schema has no arbitrary Pod spec, alternate-account or
resource-override field; the configuration fixes the namespace, and `name` must be a valid Sandbox
name there. The trusted approval view resolves the exact requested revision; a missing or stale
revision never silently resolves to a newer one, and execution materializes that configuration or
refuses before effects.

The backend binds `creation_id` to the caller UID and exact request before provisioning. An exact
retry resolves the original receipt; conflicting reuse is refused. `not_observed` means only that
the backend has no creation fact yet (expected while approval is pending), not that a timed-out
creation had no effect. `provisioning` means durable backend admission; `created` proves a Sandbox
UID exists, with readiness a separate observation; `failed` requires a known terminal failure,
otherwise `unknown` with retained facts. Backend errors return `ToolError`, never invented emptiness.
No general queue, new template CRD or storage implementation is selected by these contracts.
