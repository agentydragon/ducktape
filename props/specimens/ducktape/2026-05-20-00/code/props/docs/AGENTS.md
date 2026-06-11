# Agent-Facing Documentation

Agent-facing documentation (templates baked into container images) lives in
`props/agents/docs/`. See <../agents/docs/>.

This directory (`props/docs/`) contains **developer-facing** documentation only.

## Include Hierarchy Rule

**If template A includes template B via `include_doc()`, template A must NOT call
`describe_relation()` for tables already described in B.**

Example violation:

```mako
## grader prompt
${describe_relation("true_positives")}           ## WRONG - already in ground_truth.md.mako
${include_doc("props/agents/docs/db/ground_truth.md.mako")}  ## includes describe_relation("true_positives")
```

The grader would see the `true_positives` schema twice.

**Correct approach:** Only call `describe_relation()` for tables unique to the current template.
Let included docs handle their own tables.

## Mako Patterns

Templates use these helpers (defined in `props/agents/runtime.py`):

| Pattern                          | Purpose                                                 |
| -------------------------------- | ------------------------------------------------------- |
| `${describe_relation("name")}`   | Outputs table schema from SQLAlchemy metadata           |
| `${include_doc("package/path")}` | Includes another template from Python package resources |
| `${include_file("/path")}`       | Includes file from filesystem                           |

## Write for Agents

**Audience:** The agent running in a container, not developers reading source code.

**How docs reach agents:** Agent main loops use `render_system_prompt()` to render Mako
templates. Output goes to the agent's system prompt.

**Example - wrong:**

> BootstrapHandler checks for TruncatedStream in the BaseExecResult and raises InitFailedError.

**Example - right:**

> Init output must stay under `mcp_infra.exec.models.MAX_BYTES_CAP`. If exceeded, the agent run fails.
