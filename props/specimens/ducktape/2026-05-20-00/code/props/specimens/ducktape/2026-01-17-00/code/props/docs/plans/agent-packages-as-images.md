# Agent Packages as OCI Images

## Status: ~99% Complete - Only Base Image Optimization Remaining

## Problem

Current agent packaging has an awkward intermediate step:

1. Agent packages are tarballs containing Dockerfile + build context
2. Tarballs stored in PostgreSQL
3. At launch time, tarball extracted and `docker build` runs
4. Only then does the container run

This adds latency, complexity, and makes it harder for agents to iterate on images.

## Goal

Agent packages ARE OCI images directly. No Dockerfile build step at launch time.

## Decisions

### Registry Location

**Decision: Devenv-managed local registry** (like we do for postgres).

- Standard Docker registry container on shared Docker network
- Managed by devenv/process-compose alongside postgres
- For production: can swap to remote registry (GHCR, etc.) via config

### Agent Interface

**Decision: Direct registry access via standard OCI Distribution Protocol, through an immutable proxy.**

Agents use standard tools (curl, Python requests, crane) to interact with the registry.
No custom MCP wrapper needed - the OCI protocol is simple enough.

A proxy sits between agents and the registry (see Architecture section for network details).

All services (postgres, registry, proxy) run as Docker containers. Agents access the proxy at `http://registry-proxy:5050`.

**Security requirement:** Agent containers must NOT have direct access to the registry. They can only reach the registry through the proxy. This ensures ACL and audit controls cannot be bypassed.

**Host access:** The host machine pushes builtin images through the proxy (not direct to registry). This ensures the proxy writes all `agent_definitions` rows.

The proxy has two modes:

- **Agent mode** (default): digest-only pushes, no tags, validates agent auth against postgres
- **Admin mode**: allows tag pushes (e.g., `critic:builtin`), validates admin user against postgres

Both modes use the same postgres-based auth - admin is just another user with elevated permissions.

Bazel `oci_push` goes through proxy with admin auth → proxy writes `agent_definitions` row → forwards to registry.

Host normally accesses registry through the proxy with admin auth. Direct registry access (`localhost:5000`) is available for low-level debugging if needed.

### Immutable, Digest-Only References

**Decision: Agents can only push manifests by digest, not by tag.**

This enforces immutability:

- **Allowed:** `PUT /v2/<name>/manifests/sha256:abc123...`
- **Blocked:** `PUT /v2/<name>/manifests/latest`, `PUT /v2/<name>/manifests/v2`

Benefits:

- No naming conflicts or "who owns this tag?" questions
- No ACL complexity around tag overwrites
- Content-addressed everything - push content, get hash, done
- Every `agent_run` points to exactly the image that ran

Tags (like `critic:builtin`) are set administratively for built-in images, not by agents.

### Proxy Responsibilities

The proxy:

- Validates credentials against postgres (both agent temp users and admin)
- Determines caller type from username pattern:
  - Admin: `postgres` user
  - Agent: `agent_{run_id}` pattern → query postgres for agent type
- Enforces ACL based on caller type
- Writes `agent_definitions` row on every manifest push
- Passes valid requests through to registry

**ACL by caller type:**

| Caller                | Read | Push by digest | Push by tag | Delete |
| --------------------- | ---- | -------------- | ----------- | ------ |
| Admin (postgres user) | ✓    | ✓              | ✓           | ✗      |
| PO/PI agent           | ✓    | ✓              | ✗           | ✗      |
| Critic/grader agent   | ✗    | ✗              | ✗           | ✗      |

**Proxy routing rules (after ACL check):**

| Method   | Path                                     | Action                                              |
| -------- | ---------------------------------------- | --------------------------------------------------- |
| `GET`    | `*`                                      | Pass through                                        |
| `POST`   | `/v2/<name>/blobs/uploads/`              | Pass through                                        |
| `PATCH`  | `<upload-url>`                           | Pass through                                        |
| `PUT`    | `/v2/<name>/blobs/uploads/<uuid>?digest` | Pass through                                        |
| `PUT`    | `/v2/<name>/manifests/<digest>`          | Write `agent_definitions`, pass through             |
| `PUT`    | `/v2/<name>/manifests/<tag>`             | Admin only: write `agent_definitions`, pass through |
| `DELETE` | `*`                                      | **Block** (all callers)                             |

Digest detection: references matching `^sha256:[a-f0-9]{64}$` (or other hash algos) are digests.

**Agent type inference:** The `<name>` in the URL path is the repository name, which maps directly to `agent_type_enum` (e.g., `critic`, `grader`, `prompt-optimizer`). The proxy uses this to populate `agent_definitions.agent_type`.

This keeps the registry dumb (just blob storage) while postgres is source of truth for definitions/audit.

### Image Size

**Decision: Accept 250MB hermetic Python for now.**

The Bazel hermetic build bundles libpython (250MB). Accept this tradeoff for reproducibility.
Revisit if it becomes a bottleneck.

### Image Inheritance

**Decision: No explicit inheritance API.**

Agents are expected to understand OCI/Docker layering. They can:

1. Pull existing image
2. Create new layer (tar of additional files)
3. Push new manifest referencing base layers + new layer

We provide recipes in agent prompts. No special tooling.

### Naming Convention

**Decision: Repository names (`<name>`) are agent types.**

- `critic` - critic agents
- `grader` - grader agents
- `prompt-optimizer` - prompt optimizer agents

Built-in (Bazel-built) images use the `builtin` tag:

- `critic:builtin` - the default critic image
- `grader:builtin` - the default grader image

These tags are set administratively when Bazel pushes to the registry, not by agents.

### API Changes (Implemented)

- `agent_runs.agent_definition_id` → `agent_runs.image_digest` (stores OCI manifest digest)
- Launch APIs accept `image_ref` parameter (tag or digest), resolve to digest, store digest in DB
- Tags exist for convenience (`critic:builtin`), digests for immutability (stored in DB)
- Launch flow: `image_ref` → `resolve_image_ref()` → digest → store in DB → pull image → run container

## Current Progress

- `props/core/agent_defs/critic/BUILD.bazel` - Bazel OCI build for critic agent
- Uses `py_binary` with `pkg_tar(include_runfiles=True)` to bundle Python deps
- Layers onto `python:3.12-slim` base
- Works: `bazelisk run //props/core/agent_defs/critic:load`

## Built-in Image Publishing

Built-in agent images (critic, grader, etc.) are built by Bazel using `rules_oci` and pushed to the registry through the proxy with `builtin` tag.

**Example workflow:**

```bash
# Configure docker credentials (uses postgres admin user)
docker login localhost:5050 -u "$PGUSER" -p "$PGPASSWORD"

# Push builtin image
bazelisk run //props/core/agent_defs/critic:push
```

The proxy validates admin credentials, writes an `agent_definitions` row, and forwards to the registry.

See `props/core/agent_defs/*/BUILD.bazel` for `oci_image` and `oci_push` target definitions.

## OCI Distribution Protocol

HTTP-based REST API. Repository names (`<name>`) are agent types: `critic`, `grader`, `prompt-optimizer`.

**Key endpoints:**

- `HEAD /v2/<name>/manifests/<reference>` - Check existence, get digest
- `GET /v2/<name>/manifests/<reference>` - Get manifest (by tag or digest)
- `GET /v2/<name>/blobs/<digest>` - Get layer blob
- `POST /v2/<name>/blobs/uploads/` - Start blob upload
- `PUT /v2/<name>/manifests/<digest>` - Push manifest by digest

See [OCI Distribution Spec](https://github.com/opencontainers/distribution-spec) for full protocol details. Implementation examples are in `props/core/oci_utils.py` and `props/registry_proxy/proxy.py`.

## Architecture

### Docker Networks

Two Docker networks provide isolation:

**`props-internal`** - Contains: registry, proxy, postgres

- Registry (:5000) only reachable from this network (and host via port mapping)
- Agents cannot access this network

**`props-agents`** - Contains: proxy, postgres, agent containers

- Agents can reach proxy (registry-proxy:5050) and postgres (props-postgres:5432)
- Agents cannot reach registry directly

The proxy container is attached to both networks, bridging them.

### Host Access

- **Proxy** (`localhost:5050`): Primary access point. Admin auth for all operations (push builtins, pull images for launch)
- **Registry** (`localhost:5000`): Direct access available for debugging/inspection if needed, but normal workflow uses proxy

### Agent Workflows

**PO/PI agents (registry access via proxy):**

1. Pull `critic:builtin` via proxy (`registry-proxy:5050`)
2. Create new layer with modified prompt/code
3. Push manifest by digest (proxy writes `agent_definitions` row)
4. New digest returned, recorded in `agent_run.image_digest`

**Critic/grader agents (no registry access):**

- Launch infrastructure (host) pulls image via proxy with admin auth
- Agent container runs with pre-pulled image
- No proxy access granted to critic/grader containers

## Migration Summary

The migration to OCI images is substantially complete. Phases completed:

1. **Schema migration** - `agent_runs.image_digest` stores manifest digests, `agent_definitions` table migrated
2. **Infrastructure** - Registry, proxy, postgres all managed by devenv with network isolation
3. **Agent builds** - Critic, grader, prompt-optimizer bazelized with `oci_image` + `oci_push` targets
4. **Tag resolution** - `resolve_image_ref()` in `props/core/oci_utils.py` resolves tags to digests via proxy
5. **Auth unification** - Basic auth for all callers (admin + agents), validated against postgres
6. **Cleanup** - All tarball-based code removed, deprecated stubs deleted

Remaining work is primarily runtime testing and edge case handling (see Implementation Status below).

## Key Implementation Files

| File                                  | Purpose                                               |
| ------------------------------------- | ----------------------------------------------------- |
| `props/registry_proxy/proxy.py`       | Registry proxy with ACL enforcement and postgres auth |
| `props/core/oci_utils.py`             | Tag resolution and OCI utilities                      |
| `props/core/agent_setup.py`           | Base agent environment with image pulling             |
| `props/core/agent_registry.py`        | Launch orchestration with tag resolution              |
| `props/devenv.nix`                    | Devenv config for registry + proxy + postgres         |
| `props/core/agent_defs/*/BUILD.bazel` | Bazel OCI image + push targets for each agent type    |

See git history (commits 52355cfb through 01c2a79d) for detailed migration changes.

## Future Considerations

### Snapshot Storage in Docker Volumes

Currently snapshots (source code for evaluation) are tarballs in PostgreSQL, extracted at agent launch.

Alternative: Store snapshot content in named Docker volumes, mount read-only at `/workspace`.

Pros:

- No extraction step at launch
- Potentially more compact (shared layers if using overlay)

Cons:

- Docker API doesn't expose volume contents (can't "read file from volume")
- Would need pre-population mechanism (container that unpacks tar into volume)
- Volumes are local to Docker host - doesn't work across machines without NFS/similar
- Agents would need Docker socket access or we handle mounts at launch time

**Decision: Not pursuing now.** Current "tar in DB, extract at launch" is simple and works.
Revisit if extraction latency becomes a bottleneck.

## References

- [rules_oci](https://github.com/bazel-contrib/rules_oci) - Bazel rules for OCI containers
- [rules_pkg](https://github.com/bazelbuild/rules_pkg) - `pkg_tar` for creating layers
- [OCI Image Spec](https://github.com/opencontainers/image-spec) - Image manifest, layers, config
- [OCI Distribution Spec](https://github.com/opencontainers/distribution-spec) - Registry API (push/pull)
- [crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane) - CLI for registry operations
- [Docker Registry](https://docs.docker.com/registry/) - Reference registry implementation
- Current implementation: `props/core/agent_defs/critic/BUILD.bazel`

## Implementation Status (2026-01-09)

### ✅ Completed (~90%)

**Schema & Database**

- ✅ `agent_definitions` table migrated to digest-based primary key
- ✅ `agent_runs.image_digest` stores OCI manifest digests (sha256:...)
- ✅ All tarball support removed (archive column, build functions, CLI commands)
- ✅ RLS policies updated to work with image digests

**Infrastructure**

- ✅ OCI registry container in devenv (port 5000, props-internal network only)
- ✅ Registry proxy in devenv (port 5050, bridges props-internal + props-agents networks)
- ✅ Proxy auto-builds on `devenv up` if image missing
- ✅ Network isolation enforced (agents can't reach registry directly)
- ✅ Postgres accessible from both networks

**Authentication & Authorization**

- ✅ Postgres credential validation for all callers (`_validate_postgres_credentials`)
- ✅ Basic auth only (Bearer token support removed)
- ✅ Admin identified by postgres admin username
- ✅ Agents identified by `agent_{run_id}` username pattern
- ✅ ACL enforcement based on caller type (admin/PO/PI can push, critic/grader cannot)
- ✅ Agent definitions tracking (proxy writes DB row on manifest push)

**Tag Resolution**

- ✅ `resolve_image_ref(agent_type: AgentType, ref: str) -> str` in `props/core/oci_utils.py`
- ✅ Takes `AgentType` enum (not string) with `AGENT_TYPE_TO_REPOSITORY` mapping
- ✅ Resolves tags to manifest digests via proxy HEAD request
- ✅ AgentRegistry methods updated to call `resolve_image_ref()`
- ✅ Digests stored in `agent_runs.image_digest` column

**Agent Environments**

- ✅ CriticAgentEnvironment accepts `image_digest` parameter (removed redundant `definition_id`)
- ✅ GraderAgentEnvironment accepts `image_digest` parameter
- ✅ PromptOptimizerEnvironment accepts `image_digest` parameter
- ✅ All environments reconstruct full OCI ref: `{REGISTRY_HOST}:{REGISTRY_PORT}/{repo}@{digest}`

**Agent Builds**

- ✅ Critic agent bazelized (`oci_image` + `oci_push` targets)
- ✅ Grader agent bazelized
- ✅ Prompt-optimizer agent bazelized
- ✅ All agents build successfully with `bazel build`

**Code Cleanup**

- ✅ Deprecated tarball stubs deleted (`sync_agent_definitions_to_db`, `build_definition_images`)
- ✅ `resolve_image_id()` wrapper removed (squashed into `_resolve_image_ref()`)
- ✅ `build_images` flag removed from sync commands
- ✅ `is_digest()` deduplicated (moved to oci_utils)

### Remaining Work (~1%)

**Critical for Runtime**

- ✅ SnapshotGraderAgentEnvironment has `image` parameter (props/core/grader/snapshot_grader_env.py:52)
- ✅ ImprovementAgentEnvironment has `image` parameter (props/core/prompt_improve/improve_agent.py:81)
- ✅ Test fixtures updated (all e2e tests passing)

**Runtime Testing**

- ✅ `devenv up` with full stack implemented (devenv.nix lines 44-152: postgres, registry, registry_proxy, pg_backup)
- ✅ Network isolation verified (registry on props-internal only, agents on props-agents, proxy bridges both)
- ⚠️ Push targets exist but not CI-tested (8 targets: critic, grader, improvement, prompt_optimizer + 4 critic variants)
- ✅ E2e tests pass (props/core/test_agent_pkg_e2e.py PASSED in 27.7s)

**Agent Registry Access** (for PO/PI agents)

- ✅ Docker auth configured (agents use Basic auth with PGUSER/PGPASSWORD from db_conn.to_env_dict())
- ✅ Auth credentials passed via environment (docker_env.py:111 sets DB env vars)
- ✅ PO/PI agents can pull/push images (test_e2e.py tests full workflow: pull manifest, upload blob, push manifest)

**Agent Builds**

- ✅ 9 agent builds complete:
  - Core: critic, grader, improvement, prompt_optimizer
  - Variants: contract_truthfulness, dead_code, flag_propagation, high_recall, verbose_docs

**Lower Priority**

- ❌ Common base image for Python packages (reduce duplication)
- ✅ Documentation updates complete:
  - authoring_agents.md.j2: Updated image list (all 9 images with correct names)
  - agent_defs/AGENTS.md: Fixed agent list, added verbose_docs, corrected file references
  - agent_infrastructure.md: Removed clustering references, updated structure

### E2E Testing Requirements

**Test: PO/PI Agent Creates Custom Critic** (props/core/test_agent_pkg_e2e.py)

This test must exercise the complete agent-creates-agent workflow using only affordances available to real PO/PI agents:

1. **Pull existing agent definition**
   - PO/PI agent pulls existing critic manifest via proxy HTTP API
   - Uses Python `requests` library with Basic auth
   - Gets manifest from `GET /v2/critic/manifests/latest`

2. **Create custom variant**
   - Generate unique random token (prevents cross-test interference - each run creates different agent.md)
   - Create modified agent.md containing the random token
   - Create new OCI layer with the custom content
   - Compute manifest digest

3. **Push via proxy**
   - Push manifest by digest via `PUT /v2/critic/manifests/{digest}`
   - **Proxy automatically creates `agent_definitions` row** (agent doesn't create it manually)
   - Agent uses actual CLI commands (python -c '...', curl) in container via `docker_exec_roundtrip()`

4. **Launch custom agent**
   - Test harness triggers new critic run with custom digest
   - Uses standard test method: `test_registry.run_critic(definition_id=custom_digest)`

5. **Verify custom agent.md was used**
   - Custom critic mock checks system message contains the random token
   - This verifies the new agent.md content was actually loaded
   - Custom critic submits zero-issues critique

6. **Verify output**
   - Test checks custom critic completed successfully
   - (Future) PO/PI agent uses psql to read custom critic's output

**Key Requirements:**

- Test must ONLY use affordances available to PO/PI agents (CLI/Python commands in container)
- Mock drives sequence of commands via coroutine steps
- Random token ensures each test run uses different agent.md (no cross-test interference)
- Proxy creates `agent_definitions` automatically (agent doesn't call DB directly)
- Verifies full Docker interaction, registry flow, agent definition creation, and launching

**Test for ACL Enforcement** (test_critic_cannot_push_images)

- Critic agent attempts to push manifest to proxy
- Should receive 403 Forbidden (critics have no registry write access)
- Verifies no `agent_definitions` rows were created

### Key Design Decisions

1. **Manifest digest as identifier**: Store OCI manifest digest (sha256 of manifest JSON), not Docker Image ID (sha256 of config blob). Manifest digest is the standard OCI identifier for registry operations.
2. **Network isolation enforced**: Two Docker networks prevent ACL bypass (agents can't reach registry directly)
3. **Postgres validates all auth**: No hardcoded credentials, all validated against DB via connection testing
4. **Tags for convenience, digests for immutability**: Launch by tag (e.g., `critic:latest`), store digest in DB
5. **Proxy auto-builds**: No manual setup required for devenv
6. **AgentType enum for type safety**: `resolve_image_ref()` takes typed enum, not strings

## Design: ID Types and Resolution

### Builtin Image Tag

**Constant:** `BUILTIN_TAG = "latest"`

All built-in images pushed from Bazel targets use the `latest` tag. This is the single source of truth for "official" agent images.

### ID Types

| ID Type             | Format                       | Where It Lives                        | Purpose                   |
| ------------------- | ---------------------------- | ------------------------------------- | ------------------------- |
| **Tag**             | `"latest"`, custom tags      | User input (critic only), Bazel       | Human-friendly references |
| **Digest**          | `"sha256:abc123..."`         | Database (`agent_runs.image_digest`)  | Immutable content address |
| **Full OCI Ref**    | `"host:port/repo@digest"`    | Runtime only (passed to Docker)       | Container execution       |
| **Repository Name** | `"critic"`, `"grader"`, etc. | Derived from `AgentType` via `str(x)` | Registry namespace        |
| ~~definition_id~~   | ~~"critic"~~                 | **DEPRECATED** - remove               | Replaced by `AgentType`   |
| ~~image_digest~~    | ~~passed to environment~~    | **DEPRECATED** - pass full ref        | Use `image` parameter     |

### Repository Name Mapping

**Trivial mapping:** `repository = str(agent_type)` (lowercase string from enum)

```python
AgentType.CRITIC → "critic"
AgentType.GRADER → "grader"
AgentType.PROMPT_OPTIMIZER → "prompt_optimizer"
AgentType.IMPROVEMENT → "improvement"
```

No lookup table needed - the enum value IS the repository name.

### Layer Separation

#### Layer 1: High-Level API (AgentRegistry)

**Responsibilities:**

- Knows agent types, tags, resolution semantics
- Decides which agents allow custom images vs builtin-only
- Resolves tags → digests
- Constructs full OCI references
- Stores digests in database

**Critic launch** (custom image allowed):

```python
async def run_critic(
    self,
    *,
    snapshot_slug: SnapshotSlug,
    client: OpenAIModelProto,
    image_ref: str,  # REQUIRED - must be explicit
) -> UUID:
    # 1. Resolve tag → digest
    image_digest = resolve_image_ref(AgentType.CRITIC, image_ref)

    # 2. Build full OCI reference
    full_image = build_oci_reference(AgentType.CRITIC, image_digest)

    # 3. Store digest in DB
    session.add(AgentRun(image_digest=image_digest, ...))

    # 4. Pass full reference to environment
    env = CriticAgentEnvironment(
        ...,
        image=full_image,  # Full URI
    )
```

**Grader/optimizer/improver launch** (builtin-only):

```python
async def run_grader(
    self,
    *,
    critic_run_id: UUID,
    client: OpenAIModelProto,
    # NO image_ref parameter - always uses builtin
) -> UUID:
    # Always resolve from builtin tag
    image_digest = resolve_image_ref(AgentType.GRADER, BUILTIN_TAG)
    full_image = build_oci_reference(AgentType.GRADER, image_digest)

    env = GraderAgentEnvironment(..., image=full_image)
```

**Why this split?**

- Critic agents benefit from custom prompts/images (experimentation)
- Grader/optimizer/improver are infrastructure - should be stable and consistent
- Prevents accidental image mismatches in evaluation pipeline

#### Layer 2: Agent Environment (Container Management)

**Responsibilities:**

- Docker container lifecycle
- Type-specific MCP server creation (via `_make_mcp_server()`)
- Container configuration (labels, mounts, etc.)

**Does NOT know:**

- Agent types (beyond what's needed for MCP server)
- Tag resolution
- Registry host/port
- Image URI construction

```python
class AgentEnvironment(ABC):
    def __init__(
        self,
        agent_run_id: UUID,
        docker_client: aiodocker.Docker,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        *,
        image: str,  # Full OCI reference - ONLY identifier
        container_name: str | None = None,
        labels: dict[str, str] | None = None,
        auto_remove: bool = False,
    ):
        # No definition_id, no digest, no agent_type
        self._image = image
        # ... rest

    @abstractmethod
    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        """Subclasses provide type-specific MCP server."""
        pass
```

**Subclass example:**

```python
class CriticAgentEnvironment(AgentEnvironment):
    def __init__(
        self,
        critic_run_id: UUID,
        snapshot_slug: SnapshotSlug,
        docker_client: aiodocker.Docker,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        *,
        image: str,  # Pre-resolved full reference
    ):
        self._snapshot_slug = snapshot_slug

        super().__init__(
            agent_run_id=critic_run_id,
            docker_client=docker_client,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image=image,  # Just pass through
            container_name=f"critic-{short_uuid(critic_run_id)}",
            labels={"adgn.role": "critic", ...},
        )

    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        return CriticSubmitServer(
            critic_run_id=self._agent_run_id,
            snapshot_slug=self._snapshot_slug,
            auth=auth,
        )
```

**Key insight:** Agent environments still have type-specific behavior (MCP servers), but they don't handle image resolution/construction - that's the API layer's job.

#### Layer 3: Resolution Utilities (OCI Utils)

**Centralized** construction and resolution:

```python
# props/core/oci_utils.py

# Builtin image tag constant
BUILTIN_TAG = "latest"

def resolve_image_ref(agent_type: AgentType, ref: str) -> str:
    """Resolve tag or digest to canonical digest.

    Args:
        agent_type: Type of agent (for repo name)
        ref: Tag like "latest" or digest "sha256:..."

    Returns:
        Canonical digest "sha256:..."
    """
    if is_digest(ref):
        return ref

    repository = str(agent_type)  # Trivial mapping!
    # HEAD request to resolve tag...
    return digest

def build_oci_reference(agent_type: AgentType, digest: str) -> str:
    """Build full OCI reference from agent type and digest.

    Args:
        agent_type: Type of agent
        digest: Manifest digest "sha256:..."

    Returns:
        Full reference "localhost:5050/critic@sha256:..."
    """
    repository = str(agent_type)
    return f"{REGISTRY_HOST}:{REGISTRY_PORT}/{repository}@{digest}"
```

### Resolution Flow

```
User/API Layer (AgentRegistry):
  agent_type=CRITIC, ref="latest" (or custom for critic)
          ↓
[resolve_image_ref(CRITIC, "latest")]
          ↓
      digest="sha256:abc..."
          ↓
    [Store in DB: agent_runs.image_digest]
          ↓
[build_oci_reference(CRITIC, digest)]
          ↓
full_ref="localhost:5050/critic@sha256:abc..."
          ↓
  [Pass to Environment]
          ↓
    CriticAgentEnvironment(image=full_ref)
          ↓
    Docker pull/run
```

### DRY Guarantees

1. **URI construction:** ONE function (`build_oci_reference()`)
2. **Tag resolution:** ONE function (`resolve_image_ref()`)
3. **Repository mapping:** ONE expression (`str(agent_type)`)
4. **Builtin tag:** ONE constant (`BUILTIN_TAG`)
5. **Registry host/port:** ONE place (`REGISTRY_HOST`, `REGISTRY_PORT` in `registry/images.py`)

### Image Reference Policy

| Agent Type       | Custom Image Allowed? | Default              | Rationale                          |
| ---------------- | --------------------- | -------------------- | ---------------------------------- |
| Critic           | ✅ Yes (required arg) | User must specify    | Experimentation on prompts         |
| Grader           | ❌ No                 | Always `BUILTIN_TAG` | Evaluation infrastructure (stable) |
| Prompt Optimizer | ❌ No                 | Always `BUILTIN_TAG` | Infrastructure agent (stable)      |
| Improvement      | ❌ No                 | Always `BUILTIN_TAG` | Infrastructure agent (stable)      |
| Snapshot Grader  | ❌ No                 | Always `BUILTIN_TAG` | Long-running daemon (stable)       |

**Critic special case:** Researchers want to test modified prompts/code, so custom images are useful. All other agents are infrastructure that should stay consistent.
