# agent_pkg - Agent Packages

Infrastructure for **agent packages** — Docker images that define agents running within dedicated containers.

## Concept

An **agent package** is a Docker image that runs a self-contained agent loop:

- The container starts via `CMD` and runs its own agent loop
- The agent talks to the LLM proxy via `OPENAI_BASE_URL`
- Tools are executed via subprocess inside the container
- The container exits 0 on success, non-zero on failure

## Modules

- **mcp.py** — MCP client helpers for connecting to MCP servers from within containers
- **output.py** — Output formatting (`print_section`, `run_command`, `print_file`, `render_doc`) and Mako template rendering

Minimal dependencies since this is installed separately in container images.

## Users

- **editor_agent** uses `agent_pkg` for in-container utilities.
- **props** has its own agent infrastructure and does not depend on `agent_pkg`.
