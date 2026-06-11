# Agent Package Authoring Guide

This guide is for agents that **dynamically create** agent packages (e.g., prompt optimizer creating custom critics). It covers the required structure, security constraints, and patterns for building effective packages.

## Overview

Agent packages are stored as **tarballs (Docker build contexts)** in the database. Each tarball contains:

- **Dockerfile** — Build recipe (required)
- **init** — Bootstrap script that outputs system prompt (must be `/init` in image)
- **agent.md** — Optional agent-specific prompt portion (used by some /init implementations)
- **Python packages** — Bundled dependencies with their docs

**Image contract:** Built Docker image must have `/init` (executable) that outputs the system prompt to stdout.

## Common Workflow

```bash
# Fetch base package (unpacks full tarball including Dockerfile)
props agent-pkg fetch <id> /workspace/my_critic/

# Modify agent.md (and optionally Dockerfile, init, packages)
# You have full control — can modify anything

# Pack and insert new package
props agent-pkg create /workspace/my_critic/
```

## Required Files

### `Dockerfile` — Build Recipe

Your Dockerfile must produce an image with `/init` as an executable.

### `init` — Bootstrap Script

**MUST be at `/init` in the image and be executable** (`chmod 0o755`).

Runs BEFORE the agent's first sampling turn. Its stdout becomes the system prompt.

**Purpose:**

1. Verify environment and preconditions
2. Print the complete system prompt to stdout
3. Fail fast if something is wrong (exit non-zero)

**Critical: Exit non-zero on failures:**

```python
#!/usr/bin/env python3
import sys
from pathlib import Path
from sqlalchemy import text
from props_core.db import get_session

# 1. Verify RLS context (CRITICAL)
with get_session() as session:
    agent_run_id = session.execute(text("SELECT current_agent_run_id()")).scalar()
    if not agent_run_id:
        print("ERROR: current_agent_run_id() is NULL - RLS will block writes", file=sys.stderr)
        sys.exit(1)

# 2. Verify expected paths exist
snapshot_path = Path("/snapshots/my-snapshot")
if not snapshot_path.is_dir():
    print(f"ERROR: Snapshot not found: {snapshot_path}", file=sys.stderr)
    sys.exit(1)

# 3. Print system prompt to stdout
print("You are a code review assistant. Review the code at /snapshots/my-snapshot.")
print("Call the submit tool when done.")
```

**Must exit non-zero if:**

- `current_agent_run_id()` returns NULL (RLS will silently block all writes)
- Required directories don't exist
- Database connection fails
- MCP resources are unreadable
- Any other precondition fails

## Output Size Limit

Your init script output must stay under the limit defined by `mcp_infra.exec.models.MAX_BYTES_CAP` (stdout + stderr combined). Check the current value:

```python
from mcp_infra.exec.models import MAX_BYTES_CAP
print(f"Limit: {MAX_BYTES_CAP} bytes ({MAX_BYTES_CAP // 1000}KB)")
```

**If exceeded:** The agent run fails immediately.

**To stay under the limit:**

1. Don't print large files directly — docs are in Python packages for on-demand reading
2. Print summaries, not full content (e.g., file counts, not file contents)
3. Use `render_agent_prompt()` which handles common patterns efficiently

## Optional Files

### `agent.md` — Agent-Specific Prompt

Some `/init` implementations use an `agent.md` file for the customizable portion of the prompt:

```python
#!/bin/sh
# /init
exec props critic-agent init /agent.md
```

The `critic-agent init` command renders the base critic scaffold and includes the `/agent.md` content.

### `bin/` — CLI Tools

Custom tools the agent can invoke via shell. Useful for:

- Structured output (parse tool output into JSON)
- Multi-step operations (run analysis, filter, format)
- Database operations (wrap complex SQL)
- Validation (check work before submission)

### Python Package Docs

Documentation lives in Python packages (e.g., `props_agent_util/docs/`). The `render_agent_prompt()` function reads docs from package resources and renders them.

## Security: No External Symlinks

**Agent-created packages cannot use symlinks to files outside the package directory.**

This prevents directory escape attacks (e.g., symlinking to `/etc/passwd`).

When you pack a package with `pack_agent_pkg(path)`:

- External symlinks raise `ValueError`
- Internal symlinks (within the package) are allowed
- Dockerfile must be present

## Reusing Files from Base Packages

Use the CLI to fetch and modify base packages:

```bash
# Fetch base package (includes Dockerfile, init, agent.md if present)
props agent-pkg fetch <id> /workspace/my_critic/

# Modify what you need
# Edit agent.md, Dockerfile, init, etc.

# Pack and insert
props agent-pkg create /workspace/my_critic/
```

For programmatic access, see the `props_core.agent_pkg_utils` module.

## Container Environment

At runtime, the built Docker image has:

```
/                              # Container root
├── init                       # Executable bootstrap script (outputs system prompt)
├── agent.md                   # Optional agent-specific prompt (if used)
└── ...                        # Installed Python packages

/snapshots/{slug}/             # Source code (fetched by init at runtime)
/workspace/                    # Working directory (empty at start, bind-mounted)
```

For database access, MCP connection, and RLS scoping, see the docs in `props_agent_util`.

## Package Helpers

Use the CLI (preferred):

```bash
props agent-pkg fetch <id> /workspace/my_def/   # unpack base
props agent-pkg create /workspace/my_def/       # pack and insert
```

Or Python API (see `props_core.agent_pkg_utils` for details):

```python
from props_core.agent_pkg_utils import pack_agent_pkg, unpack_agent_pkg

archive = pack_agent_pkg(my_dir)  # validates Dockerfile exists
unpack_agent_pkg(archive, target_dir)
```

## Best Practices

1. **Init fails fast** — Check every precondition. Exit non-zero immediately on failure.

2. **Init outputs system prompt** — Everything the agent needs in the first message.

3. **Custom tools for structured work** — Instead of parsing raw output, write tools that structure it.

4. **Use the CLI pattern** — Typer-based CLIs in `bin/` give clear, documented commands.

5. **Verify before submit** — Tools can validate work. E.g., list reported issues before final submit.

6. **Design for your task** — There's no required I/O pattern. Use whatever fits.

## Common Patterns

### Minimal Custom Critic

```
my_critic/
├── Dockerfile        # Build recipe (COPY init, install packages)
├── init              # Verify snapshot, DB, output system prompt
└── agent.md          # Optional: agent-specific prompt portion
```

### Full-Featured Agent

```
my_agent/
├── Dockerfile        # Build recipe
├── init              # Environment verification + system prompt output
├── agent.md          # Agent-specific instructions
├── bin/
│   ├── analyze       # Run analysis
│   ├── report        # Generate reports
│   └── submit        # Final submission
└── props_agent_util/ # Bundled package with docs
```

## Validation

The `pack_agent_pkg()` function validates that Dockerfile exists. The image build validates that `/init` is present in the final image.
