# Agent Packages

This directory contains agent packages deployed as OCI images to containers.

## Agent-Facing Documentation

@../docs/AGENTS.md

## Definition Authoring

@../docs/authoring_agents.md.j2

## Agent Types

**Primary agents:** `critic/`, `grader/`, `improvement/`, `prompt_optimizer/`

**Critic-based detectors:** `contract_truthfulness/`, `dead_code/`, `flag_propagation/`,
`high_recall_critic/`, `verbose_docs/` — share the same init bootstrap.

## OCI Image Packaging

Agent packages are built as OCI images using Bazel and pushed to the local registry.

### Building and Pushing Images

```bash
# Start the registry (from props directory)
devenv up

# Build and push agent images
bazel run //props/core/agent_defs/critic:push
bazel run //props/core/agent_defs/grader:push
bazel run //props/core/agent_defs/improvement:push
bazel run //props/core/agent_defs/prompt_optimizer:push
bazel run //props/registry_proxy:push

# Or load into local Docker for testing
bazel run //props/core/agent_defs/critic:load
bazel run //props/core/agent_defs/grader:load
bazel run //props/core/agent_defs/improvement:load
bazel run //props/core/agent_defs/prompt_optimizer:load
bazel run //props/registry_proxy:load
```

### Future Optimization: Common Base Image

All agents currently use `python_slim` base with dependencies installed at runtime via Bazel's runfiles.
Future optimization: Create a common base image with pre-installed Python packages (openai, pydantic, sqlalchemy, etc.),
then layer only agent-specific code on top. This would:

- Reduce image size duplication
- Speed up builds (shared base layer cached)
- Maintain security (base image updated once, all agents benefit)

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
