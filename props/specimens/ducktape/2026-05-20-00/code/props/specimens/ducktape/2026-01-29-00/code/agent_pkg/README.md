# agent_pkg - Agent Packages

Infrastructure for **agent packages** — Docker build contexts that define agents that run within a dedicated container.

## Concept

An **agent package** is a directory containing:

- A `Dockerfile` that builds an agent container
- An `/init` script that outputs the agent's system prompt when executed

The host builds the image, starts a container, runs `/init`, and uses its stdout as the system prompt for the agent loop.

## Image Contract

The built image must have `/init` which:

- Is executable
- Prints the system prompt to stdout
- Exits 0 on success

## Package Structure

```
agent_pkg/
├── host/      # Host-side: image building, init runner
└── runtime/   # Container-side: utilities for init scripts
```

- **host/** — Builds images from agent packages, runs `/init`, validates the image contract. Depends on `mcp-infra` (workspace member).
- **runtime/** — Minimal utilities installed in containers. Has minimal dependencies (no workspace deps) since it's installed separately in container images.
