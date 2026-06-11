# Agent Packages

## Overview

Agent packages are self-contained directories that fully specify an agent's behavior.
They are packed as tar archives containing a Dockerfile that builds an image with `/init`.

**Image contract:** The built Docker image must contain `/init` (executable).
When run, `/init` outputs the agent's system prompt to stdout.

**Identity model:**

- **Repo-backed packages**: Human-readable IDs (e.g., "critic", "grader"). Synced
  from `agent_defs/` directory, always updated in place on sync.
- **Agent-created packages**: UUIDs assigned by the MCP server (via CLI helper).
  Immutable once created.

## Package Structure

Agent packages in `props/core/agent_defs/`:

```
agent_defs/
├── critic/                      # Dockerfile, init, agent.md (optional)
├── grader/                      # Dockerfile, init
├── prompt_optimizer/            # Dockerfile, init, agent.md
├── improvement/                 # Dockerfile, init, agent.md
└── <detector>/                  # Critic-based (inherits via symlinks)
```

**Required:**

- `Dockerfile` - Builds the agent image (must produce `/init`)
- `/init` in image - Executable that outputs system prompt to stdout

**Optional (used by some /init implementations):**

- `agent.md` - Agent-specific prompt portion rendered by `/init`
- Supporting files referenced by Dockerfile

## Agent Types

```python
class AgentType(StrEnum):
    CRITIC = "critic"
    GRADER = "grader"
    PROMPT_OPTIMIZER = "prompt_optimizer"
    CLUSTERING = "clustering"
    IMPROVEMENT = "improvement"
    FREEFORM = "freeform"  # For sub-agents spawned by other agents
```

## Database Schema

**Tables:**

- `agent_definitions` - Archives with `id`, `agent_type`, `archive` bytea
- `agent_runs` - Unified runs with `agent_run_id`, `type_config` JSONB
- `events` - Tool call traces linked via `agent_run_id`
- `reported_issues`, `reported_issue_occurrences` - Critic output
- `grading_decisions` - Grader output

**Migrations:** All squashed into `20251223000000_schema_squashed.py`

## Access Control

All agents use a single `agent_base` role with RLS policies based on agent type:

- Username format: `agent_{agent_run_id}`
- Helper functions: `current_agent_run_id()`, `current_agent_type()` (SECURITY DEFINER)

| Resource          | Critic     | Grader                 | Prompt Optimizer    |
| ----------------- | ---------- | ---------------------- | ------------------- |
| Own events        | SELECT     | SELECT                 | SELECT (TRAIN only) |
| reported_issues   | INSERT own | SELECT graded          | SELECT TRAIN only   |
| Ground truth      | -          | SELECT graded snapshot | SELECT TRAIN only   |
| grading_decisions | -          | INSERT own             | SELECT TRAIN only   |

## Runtime Flow

**CRITICAL: Image must be built BEFORE agent loop starts.**

```
1. Create AgentRun in database
2. Load archive from database
3. Build Docker image from archive (via ensure_image_from_archive)
4. Enter AgentEnvironment context (starts container)
5. Create AgentHandle - runs /init and uses output as system prompt
6. Run agent loop
```

**Environment variables available to agents:**

- Database: `$PGHOST`, `$PGPORT`, `$PGUSER`, `$PGPASSWORD`, `$PGDATABASE`
- MCP: `$MCP_SERVER_URL`, `$MCP_SERVER_TOKEN`
- `$AGENT_RUN_ID` - UUID of the current agent run

## Agent CLI Commands

| Agent            | CLI          | Subcommands                                                                  |
| ---------------- | ------------ | ---------------------------------------------------------------------------- |
| Critic           | `critique`   | `insert-issue`, `insert-occurrence`, `submit`, `list-issues`, `delete-issue` |
| Grader           | `grade`      | `add-tp-match`, `add-fp-match`, `add-no-match`, `delete-decision`, `submit`  |
| Clustering       | `clustering` | `create-cluster`, `assign-to-cluster`, `assign-to-tp`, `assign-to-fp`        |
| Prompt Optimizer | `critic-dev` | `run-critic`, `run-grader`, `leaderboard`, `hard-examples`                   |

Usage: `<cli> <command> [args]` (CLIs are pip-installed console scripts)

## Future Work

### Sub-Agent Spawning (FREEFORM type)

Agents can spawn sub-agents for task decomposition. A critic might delegate
specialized analysis to sub-agents.

- Sub-agent gets own `agent_run_id` with `parent_agent_run_id` pointing to parent
- Inherits snapshot mount from parent
- Container can be restarted; transcript reconstructed from events table

### Recursive Cost Aggregation

Track costs across agent sub-trees:

- `own_cost` - Cost of this agent run only
- `cost_including_subagents` - Recursive sum including descendants

## References

- Agent packages: `props/core/agent_defs/`
- Agent runtime utilities: `agent_runtimes/` (critic_util, grader_util, etc.)
- Package building: `agent_pkg/host/src/agent_pkg_host/builder.py`
