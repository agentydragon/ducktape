# Agent Packages

This directory contains agent packages deployed as OCI images to containers.

## Agent-Facing Documentation

@../docs/AGENTS.md

## Definition Authoring

@../docs/writing_agent_definitions.md.j2

## Agent Types

**Primary agents:** `critic/`, `grader/`, `clustering/`, `improvement/`, `prompt_optimizer/`

**Critic-based detectors:** `dead_code/`, `high_recall_critic/`, `flag_propagation/`,
`contract_truthfulness/` — share the same `critique init` bootstrap.

## OCI Image Packaging

Agent packages are built as OCI images using Bazel and pushed to the local registry.

### Building and Pushing Images

```bash
# Start the registry (from props directory)
devenv up

# Build and push critic image
bazel run //props/core/agent_defs/critic:push

# Or load into local Docker for testing
bazel run //props/core/agent_defs/critic:load
```

### Registry URLs

- **Direct registry**: `http://localhost:5050` (for Bazel push, debugging)
- **Proxy with ACL**: `http://localhost:5051` (for agent access with permissions)

### Image References

Agent runs reference images by digest in the database:

```python
# Images are resolved from agent_definitions table by digest
# Proxy writes agent_definitions rows on manifest push
# agent_runs.image_digest is FK to agent_definitions.digest
```

### Network Isolation

- **props-internal network**: Registry, proxy, postgres (agents cannot access directly)
- **props-agents network**: Proxy, postgres, agent containers (agents can only reach proxy)

This ensures agents cannot bypass ACL enforcement.

### ACL Enforcement

The registry proxy enforces permissions by agent type:

| Agent Type       | Read Registry | Push by Digest | Push by Tag | Delete |
| ---------------- | ------------- | -------------- | ----------- | ------ |
| Admin            | ✓             | ✓              | ✓           | ✗      |
| Prompt Optimizer | ✓             | ✓              | ✗           | ✗      |
| Prompt Improver  | ✓             | ✓              | ✗           | ✗      |
| Critic           | ✗             | ✗              | ✗           | ✗      |
| Grader           | ✗             | ✗              | ✗           | ✗      |

Prompt optimizer and improver agents can create modified images by layering on existing ones.
Critic and grader agents have no registry access - images are pulled for them by the launch infrastructure.

## Validation

```bash
# Build OCI image
bazel build //props/core/agent_defs/critic:image

# Load and test locally
bazel run //props/core/agent_defs/critic:load
docker run --rm critic-agent:latest

# Push to registry
bazel run //props/core/agent_defs/critic:push
```
