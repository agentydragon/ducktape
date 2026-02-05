# agent_pkg - Agent Packages

Infrastructure for **agent packages** — Docker images that define agents running within dedicated containers.

## Concept

An **agent package** is a Docker image that runs a self-contained agent loop:

- The container starts via `CMD` and runs its own agent loop
- The agent talks to the LLM proxy via `OPENAI_BASE_URL`
- Tools are executed via subprocess inside the container
- The container exits 0 on success, non-zero on failure

## Package Structure

```
agent_pkg/
├── host/      # Host-side: image building (docker buildx)
└── runtime/   # Container-side: utilities for agent init and prompt rendering
```

- **host/** — Builds images from agent definition directories using `docker buildx build`. Used by both props agents (OCI images built by Bazel) and editor agents (built from repo filesystem).
- **runtime/** — Minimal utilities installed in containers for system prompt generation and output formatting. Has minimal dependencies (no workspace deps) since it's installed separately in container images.

## Props Agent Images

Props agent images are built by Bazel as OCI images (`oci_image` rules) and pushed to a registry. Agent types: critic, grader, prompt_optimizer, improvement. See <props/docs/agent-loop-inside-container.md> for the in-container architecture.

## Editor Agent Images

Editor agent images are built from directories on the host filesystem using `ensure_image()`. The editor agent uses a different architecture with a host-side agent loop — see `editor_agent/`.
