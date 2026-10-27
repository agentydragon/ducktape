# ADR: `orchestration_v1` acts for the operator until backends grant callers

- **Status:** Accepted by the operator, 2026-10-26 and 2026-10-27, as a stopgap: it "will just fill the immediate
  need but it's not a setup that's like long term a nice thing ... But it should work"
- **Date:** 2026-10-27
- **Scope:** the [`orchestration_v1` Action group](../action_service/orchestration_v1/README.md), the Session
  Dispatcher's callers, the History Service's readers, launcher grants

## Context

An agent connected through Agentplane's MCP server (claude.ai, as `claude-ai`) or running in a Session Sandbox
needs to launch worker Sandboxes, give them tasks and follow them. None of that had a caller path: launching was the
operator's, through the app; the Dispatcher admitted only the app and Notifications, trusted to act for their users;
History served only its configured readers. The rule types that would let each backend grant these operations to a
caller (Session creation, scoped sends, scoped reads, constrained launch) are open or held designs. The operator
accepted Action approval in their place: "i might be fine for now with some of these actions needing actions
approval rather than executing by some mechanism that does not need human approval".

## Decision

**Each operation is the operator's act.** The request's Decision, the operator's approval or an
`action_auto_approve` rule the operator wrote, is the authority. The executor then performs it with the Action
Service's own account, which the Dispatcher and History trust as they trust the app. The tool surface does not name
the authority, so when a backend gains a rule type, the executor calls it as the caller and the tools stay.

- **The executor lives in the Action Service,** the one service that knows the authenticated
  `ExecutionRequest.caller` a launcher grant binds and provenance records.
- **Every launch writes launcher grants,** shown on its card, so one approval per worker covers its launcher's
  later commands and reads.
- **Launcher grants auto-approve by argument shape:** this group's Actions whose `sandbox` is exactly the launched
  Sandbox's name and UID. The operator found matching on shape "a little ugly" and accepted it "out of expediency";
  the target is typed permissions of a verb over a Sandbox, checked by the backend that owns the operation.
- **Presets are rendered into the group's settings** by cdk8s from the definition the app's presets come from, so
  the executor reads no app API. A launch still takes concrete fields.
- **The caller has no inbox.** A worker's reply is its turn outcome, which the caller reads.
- **`claude-ai` is the orchestrating account.** Staging auto-approves its `list_launch_presets`; every launch waits
  for the operator.

### The exceptions it makes

Each lasts until the backend rule for its operation lands.

- The Dispatcher admits and lets wake a third trusted caller beside the app and Notifications, which acts only on a
  Decision.
- History gains a reader. A listed reader reads every Session; the executor reads only on a Decision, scoped by
  launcher grants.
- Two paths open Sessions: the app for the operator and the executor for an approved caller. Both reach the one
  Dispatcher queue.
- OAuth callers, who otherwise send from an Action box as ordinary workloads, gain Actions for what no box can do.

## Rejected

- **Backend caller grants first.** The end state, but building them first holds everything on the held read and
  Session-creation policies.
- **The app's API with the `agentplane-agent` token.** A backend calling the app breaks the dependency rule and
  grows the app.
- **An upstream MCP server with the operator's identity,** as `kubernetes_admin` is. It never learns the caller, so
  it can neither bind launcher grants to the caller nor record who launched what.
- **MCP edge tools minting a caller token per backend.** Only sending has a caller grant, the rest would get no
  approval, and an in-cluster caller would use another surface.
- **Everything from an Action box.** A box does only what its account may, so it closes none of the gaps.
- **The executor as its own service,** which would need Actions to pass it the caller, a trust channel that does
  not exist.
- **Opt-in launcher grants, or an approval per command;** a second Connection whose account holds only
  orchestration grants; an inbox for the caller in v1.
