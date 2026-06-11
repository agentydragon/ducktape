# Built-in Critic Image Internals

The built-in critic images are Bazel-built Python binaries packaged into OCI images on a debian-slim base. This describes how they work internally — useful for understanding what you're starting from and for building custom images that overlay the entry point.

The built-in critic is one possible implementation. Your custom critics can take any shape — see the parent guide for what constitutes a valid critic.

## Container Entrypoint

The image ENTRYPOINT is a Bazel-generated bash launcher:

```
ENTRYPOINT: /props/agents/critic/critic_bin
```

The launcher configures a hermetic Python venv, sets `sys.path` to include the runfiles tree, and runs `props.agents.critic.main` as a module. This means:

1. `__name__` is set to `"__main__"` in `main.py`
2. The `if __name__ == "__main__"` block executes
3. That block calls `sys.exit(asyncio.run(main()))`

**When you overlay `main.py`, your replacement must provide the same interface:**

```python
async def main() -> int:
    # Your logic here. Return 0 for success, non-zero for failure.
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

You do NOT need to set up `sys.path` — the launcher already did that. All bundled Python modules (the full `props` library, `agent_core`, `openai_utils`, etc.) are importable.

## Runfiles Layout

All code lives under the runfiles tree, which mirrors the Bazel workspace:

```
/props/agents/critic/critic_bin.runfiles/_main/
├── props/
│   ├── agents/
│   │   ├── critic/
│   │   │   ├── main.py              ← agent entrypoint (override this)
│   │   │   ├── prompt.md.mako       ← system prompt template
│   │   │   └── _critic_bin.venv/    ← hermetic Python venv
│   │   │       └── bin/python3
│   │   ├── docs/                    ← agent-facing documentation
│   │   │   ├── system_access.md
│   │   │   └── db/                  ← DB schema docs (Mako templates)
│   │   ├── runtime.py               ← template rendering, agent run helpers
│   │   └── schema.py                ← SQLAlchemy schema introspection
│   ├── db/                          ← database layer (models, queries)
│   │   └── models.py                ← all ORM table/view definitions
│   └── core/                        ← core models
├── agent_core/                      ← agent loop machinery
├── mcp_infra/                       ← exec tool implementation
└── openai_utils/                    ← LLM client utilities
```

## What the Built-in Critic Does

The built-in critic (`props.agents.critic.main`) implements a simple single-agent loop:

1. Connects to PostgreSQL via `Database.from_env()`
2. Reads its `AgentRun` config (model, example, scope)
3. Fetches the snapshot to `/workspace/`
4. Renders a Mako system prompt with helpers (`${"${describe_relation()}"}`, `${"${include_doc()}"}`)
5. Creates tool provider (exec, insert_issue, insert_occurrence, submit, etc.)
6. Runs the agent loop until `submit` or `report_failure` is called

This is a reasonable starting point, but you're free to replace any or all of it.

## Key Paths

| Path | Purpose |
|------|---------|
| `/workspace/` | Working directory, writable. Snapshots fetched here at runtime. |
| `python3` | Python interpreter (on PATH). Has `props` and all dependencies importable. |
| `/props/agents/critic/critic_bin` | Bash entrypoint launcher |
| `/props/agents/critic/critic_bin.runfiles/_main/` | Bazel workspace root — all Python source code |
| `/props/agents/critic/critic_bin.runfiles/_main/props/agents/critic/main.py` | Agent entrypoint (override this to customize) |
| `/props/agents/critic/critic_bin.runfiles/_main/props/agents/critic/prompt.md.mako` | Default system prompt template |
