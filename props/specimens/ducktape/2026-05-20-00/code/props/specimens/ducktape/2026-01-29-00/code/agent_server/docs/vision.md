# agent_server vision — autonomous, safely self‑modifying agent scaffold

agent_server aims to provide a practical scaffold for an autonomous agent that can safely improve and adapt itself over time. The core idea is a tiny, well‑reviewed "safe core" with as many behaviors as possible moved into runtime‑programmable pieces.

## Safety and approvals

- Autonomy without nagging: the agent should run without bothering the user for approvals on operations that are safe.
- Context matters: safety is often non‑trivial and depends on task, scope, and state.
- Executable policy, not checklists: auto‑approval is driven by Python predicates that can run arbitrary checks against rich context.
  - Policy shape: `ApprovalPolicy.decide(ctx) -> (PolicyDecision, reason)` executed in a constrained container.
  - See <mcp-runtime/policy-gateway.md> and <mcp-runtime/overview.md> for the proxy/middleware gate that enforces policy before tool execution.

## Human‑in‑the‑loop, but code‑first

- The agent proposes policy changes that expand (or restrict) safe autonomy; the user interactively negotiates.
- The agent translates agreements into code changes (policy source), not long natural‑language settings.
- DRY/SSOT: instead of repeating instructions in prose, the agent reads the scaffold's source directly.
  - Example: "Policy interface lives at <agent_server/src/agent_server/approvals.py>; examples under <agent_server/src/agent_server/policies/>."

## Introspection and self‑modification

- The agent’s own scaffold (loop, MCP servers, persistence) is available for:
  - inspection (read code, read state),
  - programmatic use (call tools/resources), and
  - proposing edits that a human can approve.
- Encourage emergent automation: rather than only “seatbelt tool has a templates function,” the agent can notice repeated workflows and write small Python within its container that invokes MCP tools (via the Compositor) to automate them.
  - Compositor/mounts: see <mcp-runtime/overview.md>.

## Runtime‑programmable scaffold

Traditional “hardcoded” scaffolding becomes configurable at runtime by the agent, the user, or both:

- Policy as code
  - Policy runs out‑of‑process with a stable IO contract; hot‑swappable via proposals and approvals.
  - See <mcp-runtime/policy-gateway.md> for decision mapping and evaluation.

- MCP servers
  - Allow requests to attach/detach servers at runtime (under policy). The agent can access MCP both via direct tool calls and by running Python inside its container that talks to the Compositor over loopback.
  - Resources aggregation is provided by a dedicated server; see <mcp-runtime/resources.md>.

- Agent state and conversation history
  - Prefer exposing state via MCP servers rather than raw mounts; however, read‑only SQLite mounts into the container may be used for power users.
  - The scaffold provides helpers to read specific slices safely; complex transforms live in tools.

## Principles

- Tiny safe core
  - Keep the trusted base minimal: compositing, policy gate, loop control, and auth.
  - Everything else should be swappable, replaceable, or configurable at runtime.

- Code is the contract
  - Point the agent (and the user) at source files and examples rather than maintaining duplicated prose.
  - Example paths the agent can read:
    - Policy middleware and interface: <agent_server/src/agent_server/approvals.py>, <agent_server/src/agent_server/policy_eval/>
    - Compositor: <mcp_infra/src/mcp_infra/compositor/server.py>
    - Loop control and server: <agent_core/src/agent_core/loop_control.py>, <mcp-runtime/control.md>
    - Runtime/server wiring: <agent_server/src/agent_server/server/>, <mcp-runtime/overview.md>

- Graduated autonomy
  - No binary “run everything” or “approve each thing” levers.
  - Policies encode nuanced allow/deny/ask decisions based on context, with proposals as the collaborative path to expand autonomy safely.

## Where this goes next

- Make the safe core solid and testable.
- Expose more of the scaffold via MCP with clear, typed contracts.
- Invest in authoring UX for executable policies and proposals (agent‑assisted, human‑approved).
- Encourage the agent to read and reason about its scaffold code instead of relying on drifting prose.

## How the vision maps to the runtime design

- Compositor with policy middleware: enforces executable policy before tool execution; see <mcp-runtime/overview.md> and <mcp-runtime/policy-gateway.md>.
- Resources server: centralized `resources/*` with persisted subscriptions; forwards raw notifications; see <mcp-runtime/resources.md>.
- Loop control: neutral yield tool for turn control; see <mcp-runtime/control.md>.
- Chat: initial V1 is out of scope; near‑term MCP‑native mode; see <mcp-runtime/ui-chat.md> and <mcp-runtime/matrix.md>.

### Container interaction

- The agent uses a Docker exec MCP server (server `runtime`, tool `exec`) to run commands inside the container (e.g., `rg`, `cat`). All such calls are evaluated by the policy middleware before dispatch.
- Images: reuse the same Dockerfile (`docker/runtime/Dockerfile`) for both the runtime exec container and the policy‑evaluation container; use the same runtime flags for both. You may still choose to run policy evaluation per‑call. The image includes `rg` for fast source reads.

### Terminology alignment

- "Compositor": the aggregation server exposing namespaced tools and resources.
- "Policy middleware": pre‑dispatch approvals filter inside the Compositor on `tools/call`.
- "Orchestrator/handlers": in‑proc loop control and injection logic managing turns and wake sources.
- "Resources server": dedicated MCP server for `resources/*` (no HWM/coalescing).

### Gaps and proposals

- Principals and auth scopes
  - Document principal derivation for agent vs human vs container (bearer/JWT scopes). Ensure the policy context includes the principal and deny human bypass from containers.
- Ask flow observability
  - V1 blocks and hides pending approvals from the model. Consider a Stage 2 optional mode to surface a concise “pending approvals: N” status in system messages without leaking specifics.
- Error codes
  - Centralize a documented error code namespace for `policy_denied(_continue)`, `subscribe_unsupported`, `unsubscribe_unsupported`, `forbidden` (pinned unsubscribes).
- Tests
  - Add end‑to‑end tests for: policy allow/deny/ask, container path enforcement, subscriptions persistence/hydration, and loop yield semantics.
- Glossary
- Add a small shared glossary table across docs for policy middleware, orchestrator, Compositor, Resources server to avoid drift.

### Integration roadmap

- Align with migration stages in <mcp-runtime/overview.md>:
  1. Ensure FastMCP proxy mounts everywhere (done); transitional handler approvals allowed.
  2. Install policy middleware in Compositor; add dedicated Resources server; remove approval enforcement from handlers.
  3. Optional async inbox/tool‑state resources once sync path is rock‑solid.

## Container access to scaffold source

To enable the “read the source code” behavior, the agent’s Docker container must be able to access adgn’s source code:

- Build the agent container image with the `adgn` package preinstalled (wheel or editable install) so Python can locate package files via `importlib.resources`/`pkgutil`.

We will not expose scaffold source via MCP for UI browsing. Core agent introspection reads source from the installed package inside the container; no UI code browser is required.
