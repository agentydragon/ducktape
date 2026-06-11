# Agent-Facing Documentation

This directory (`props/docs/`) is the **single source of truth** for agent-facing documentation.

## SSOT Principle

When writing documentation that agents see at runtime:

1. **Write it ONCE here** — in `props/docs/`
2. **Reference or transclude** into other locations (developer docs, AGENTS.md files)
3. **Do NOT duplicate** content between agent docs and other locations

If the same information exists elsewhere, it will drift and become inconsistent.

## Include Hierarchy Rule

**If template A includes template B via `include_doc()`, template A must NOT call
`describe_relation()` for tables already described in B.**

Example violation:

```jinja2
{# grader.md.j2 #}
{{ describe_relation("true_positives") }}           {# WRONG - already in ground_truth.md.j2 #}
{{ include_doc("props/docs/db/ground_truth.md.j2") }}  {# includes describe_relation("true_positives") #}
```

The grader would see the `true_positives` schema twice.

**Correct approach:** Only call `describe_relation()` for tables unique to the current template.
Let included docs handle their own tables.

## Directory Structure

```
docs/
├── agents/          # Per-agent prompt templates (critic.md.j2, grader.md.j2)
├── db/              # Database schema documentation
│   ├── ground_truth.md.j2    # TPs, FPs, occurrences
│   ├── examples.md.j2        # Examples table
│   ├── critiques.md.j2       # Reported issues
│   ├── grading.md.j2         # Grading decisions, metrics
│   └── evaluation_flow.md.j2 # End-to-end pipeline
├── optimization/    # Prompt optimization docs
└── *.md             # Other shared docs
```

## Jinja2 Patterns

Templates use these helpers (defined in `agent_helpers.py`):

| Pattern                             | Purpose                                                 |
| ----------------------------------- | ------------------------------------------------------- |
| `{{ describe_relation("name") }}`   | Outputs `psql \d+ name`                                 |
| `{{ include_doc("package/path") }}` | Includes another template from Python package resources |
| `{{ include_file("/path") }}`       | Includes file from filesystem                           |
| `{{ run_command("cmd") }}`          | Executes shell command, outputs result                  |
| `{{ get_grading_context() }}`       | Grader-specific context injection                       |

## Write for Agents

**Audience:** The agent running in a container, not developers reading source code.

**How docs reach agents:** CLI init commands use `render_agent_prompt()` to render Jinja2
templates. Output goes to the agent's transcript.

**Example - wrong:**

> BootstrapHandler checks for TruncatedStream in the BaseExecResult and raises InitFailedError.

**Example - right:**

> Init output must stay under `mcp_infra.exec.models.MAX_BYTES_CAP`. If exceeded, the agent run fails.

## What Goes Here

- Database schema documentation
- RLS mechanism explanations
- MCP/database access patterns
- Workflow guides and examples
- Reference material agents need during execution

## What Does NOT Go Here

- Developer tooling instructions (IDE setup, local testing)
- Infrastructure deployment details
- Internal architecture decisions not relevant to agent execution
