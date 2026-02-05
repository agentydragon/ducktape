# Agent Infrastructure: Implementation Notes

This document covers implementation details for agent infrastructure. For the full in-container architecture, see <agent-loop-inside-container.md>.

## Directory Structure

```
props/
├── critic/                 # Critic agent (main.py entry point, DirectToolProvider)
├── grader/                 # Grader agent (loop.py, daemon.py)
├── critic_dev/             # Critic-dev agents
│   ├── optimize/           # Prompt optimizer agent
│   └── improve/            # Improvement agent
├── orchestration/          # Host scaffold (agent_registry, loop_agent_env)
├── backend/                # Unified backend (LLM proxy, registry proxy, eval API)
├── db/                     # Database layer (ORM, migrations)
├── cli/                    # CLI commands
├── testing/                # Test fixtures and mocks
└── docs/                   # Documentation
```

## Agent Images

Agent images are built by Bazel as OCI images (`oci_image` rules in each agent's `BUILD.bazel`). Each image has a `CMD` that starts the agent's own loop. See <agent-loop-inside-container.md> for the full architecture.

## Testing

See `props/testing/` for shared test fixtures (database, e2e infrastructure, mocks).
