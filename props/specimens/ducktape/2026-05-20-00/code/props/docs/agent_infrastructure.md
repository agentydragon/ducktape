# Agent Infrastructure

This document covers the OCI image architecture for agents. For the in-container agent loop, see <agent_loop_inside_container.md>.

## Directory Structure

```
props/
├── critic/                 # Critic agent (main.py entry point, DirectToolProvider)
├── grader/                 # Grader agent (loop.py, main.py)
├── critic_dev/             # Critic-dev agents
│   ├── optimize/           # Critic developer (optimizer) agent
│   └── improve/            # Critic developer (improver) agent
├── orchestration/          # Host scaffold (agent_registry)
├── backend/                # Unified backend (LLM proxy, registry proxy, eval API)
├── db/                     # Database layer (ORM, migrations)
├── cli/                    # CLI commands
├── testing/                # Test fixtures and mocks
└── docs/                   # Documentation
```

## Agent Images

Agent images are built by Bazel as OCI images (`oci_image` rules in each agent's `BUILD.bazel`). Each image has a `CMD` that starts the agent's own loop.

Built-in images use the `builtin` tag (constant `BUILTIN_TAG` in `props/core/oci_utils.py`). Bazel pushes them through the registry proxy with admin auth.

Critic-dev agents (optimizer, improvement) can also create custom critic images at runtime by layering onto the base image with `crane` and pushing to the backend's registry proxy. The registry proxy automatically creates `agent_definitions` rows for pushed images.

### Image Reference Policy

| Agent Type       | Custom Image?  | Default           | Rationale                  |
| ---------------- | -------------- | ----------------- | -------------------------- |
| Critic           | Yes (required) | User must specify | Experimentation on prompts |
| Grader           | No             | `BUILTIN_TAG`     | Evaluation infrastructure  |
| Critic-dev (opt) | No             | `BUILTIN_TAG`     | Infrastructure agent       |
| Critic-dev (imp) | No             | `BUILTIN_TAG`     | Infrastructure agent       |
| Snapshot Grader  | No             | `BUILTIN_TAG`     | Long-running grader        |

### ID Types

| ID Type         | Format                    | Where It Lives                       | Purpose                   |
| --------------- | ------------------------- | ------------------------------------ | ------------------------- |
| Tag             | `"latest"`, custom tags   | User input, Bazel                    | Human-friendly references |
| Digest          | `"sha256:abc123..."`      | Database (`agent_runs.image_digest`) | Immutable content address |
| Full OCI Ref    | `"host:port/repo@digest"` | Runtime only (passed to Docker)      | Container execution       |
| Repository Name | `"critic"`, `"grader"`    | Derived from `AgentType` via `str()` | Registry namespace        |

Repository names map trivially from the `AgentType` enum: `str(AgentType.CRITIC)` = `"critic"`.

### Resolution Flow

```
AgentRegistry:
  agent_type=CRITIC, ref="latest" (or custom digest for critic)
      → resolve_image_ref(CRITIC, "latest") → digest
      → store in agent_runs.image_digest
      → build_oci_reference(CRITIC, digest) → "host:port/critic@sha256:abc..."
      → pass to AgentEnvironment(image=full_ref)
      → Docker pull/run
```

## Registry Architecture

### Docker Networks

Two Docker networks provide isolation:

**`props-internal`** — Contains: registry, proxy, postgres

- Registry (:5000) only reachable from this network (and host via port mapping)
- Agents cannot access this network

**`props-agents`** — Contains: proxy, postgres, agent containers

- Agents can reach proxy (`registry-proxy:5050`) and postgres (`props-postgres:5432`)
- Agents cannot reach registry directly

The proxy container is attached to both networks, bridging them.

### Registry Proxy

The proxy sits between agents and the registry, enforcing access control and tracking definitions.

**Responsibilities:**

- Validates credentials against postgres (both agent temp users and admin)
- Determines caller type from username pattern (`postgres` = admin, `agent_{run_id}` = agent)
- Enforces ACL based on caller type
- Writes `agent_definitions` row on every manifest push
- Passes valid requests through to registry

**ACL by caller type:**

| Caller              | Read | Push by digest | Push by tag | Delete |
| ------------------- | ---- | -------------- | ----------- | ------ |
| Admin (postgres)    | Yes  | Yes            | Yes         | No     |
| Critic-dev agent    | Yes  | Yes            | No          | No     |
| Critic/grader agent | No   | No             | No          | No     |

Agents push manifests by digest only (`PUT /v2/<name>/manifests/sha256:...`), enforcing immutability. Tags (like `critic:builtin`) are set administratively when Bazel pushes built-in images.

### Agent Workflows

**Critic-dev agents (registry access via proxy):**

1. Pull `critic:builtin` via proxy
2. Create new layer with modified prompt/code using `crane`
3. Push manifest by digest (proxy writes `agent_definitions` row)
4. New digest returned, used as `definition_id` in `run_critic` tool call

**Critic/grader agents (no registry access):**

- Host infrastructure pulls image via proxy with admin auth
- Agent container runs with pre-pulled image
- No proxy access granted

### Key Implementation Files

| File                            | Purpose                                            |
| ------------------------------- | -------------------------------------------------- |
| `props/backend/registry_proxy/` | Registry proxy with ACL enforcement                |
| `props/core/oci_utils.py`       | Tag resolution (`resolve_image_ref`) and utilities |
| `props/orchestration/`          | Launch orchestration (`agent_registry`)            |
| `props/devenv.nix`              | Devenv config for registry + proxy + postgres      |

## Testing

See <../testing/AGENTS.md> for shared test fixtures.
