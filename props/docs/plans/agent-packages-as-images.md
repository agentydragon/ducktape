# Agent Packages as OCI Images

## Status: Draft

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

### API Changes

**`agent_definition_id` → `image_digest` (immutable content address)**

- `agent_run` table: `agent_definition_id` becomes `image_digest` (e.g., `sha256:abc123...`)
- Agent definitions as a separate concept go away - an agent IS its image
- Digests only - no mutable tag references in run records
- Tags exist only for human convenience (e.g., finding `critic:builtin`)

**Launch flow:**

1. Caller specifies tag (e.g., `critic:builtin`) or digest
2. If tag: resolve to digest via proxy `HEAD /v2/critic/manifests/builtin` (admin auth)
3. Store digest in `agent_runs.image_digest`
4. Pull image by digest via proxy, run container

Launch infrastructure runs on host and uses the proxy with admin credentials.

**Field naming:**

- DB column: `image_digest` (varchar, e.g., `sha256:abc123...`)
- Python: `image_digest: str` parameter in launch APIs
- The old `DefinitionId` type becomes unnecessary

## Current Progress

- `props/core/agent_defs/critic/BUILD.bazel` - Bazel OCI build for critic agent
- Uses `py_binary` with `pkg_tar(include_runfiles=True)` to bundle Python deps
- Layers onto `python:3.12-slim` base
- Works: `bazelisk run //props/core/agent_defs/critic:load`

## Built-in Image Publishing

Built-in agent images (critic, grader, etc.) are built by Bazel and pushed to the registry with `builtin` tag.

### rules_oci Targets

rules_oci provides `oci_push` for pushing to registries:

```starlark
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_push")

oci_image(
    name = "critic_image",
    base = "@python_3_12_slim",
    tars = [":critic_layer"],
)

oci_push(
    name = "critic_push",
    image = ":critic_image",
    repository = "localhost:5050/critic",  # Proxy URL (not registry direct)
    remote_tags = ["builtin"],              # Tag to apply
)
```

Run with: `bazelisk run //props/core/agent_defs/critic:critic_push`

### Push Workflow

Built-in images push through the proxy with admin auth:

```
Bazel ──(oci_push)──> Proxy :5050 ──> Registry :5000
                         │
                         └── writes agent_definitions row
                         └── critic:builtin (digest: sha256:abc...)
                         └── grader:builtin (digest: sha256:def...)
```

**Steps:**

1. Configure docker credentials for proxy (uses existing postgres admin user):

   ```bash
   # In devenv activation or one-time setup
   docker login localhost:5050 -u "$PGUSER" -p "$PGPASSWORD"
   ```

2. Run Bazel push:

   ```bash
   bazelisk run //props/core/agent_defs/critic:critic_push
   ```

3. Proxy receives push with admin auth:
   - Validates credentials against postgres (admin user has elevated permissions)
   - Computes manifest digest from request body
   - Writes `agent_definitions` row: `(digest=sha256:abc..., agent_type=critic, created_by_agent_run_id=NULL)`
   - Forwards request to registry (registry stores the tag→digest mapping)

**BUILD.bazel target:**

```starlark
oci_push(
    name = "critic_push",
    image = ":critic_image",
    repository = "localhost:5050/critic",  # proxy URL, not registry
    remote_tags = ["builtin"],
)
```

This is administrative - happens at build/deploy time, not by agents at runtime.

### Registry Configuration

`oci_push` targets point to the proxy URL (`localhost:5050`), not the registry directly.

For local devenv, hardcoding `localhost:5050` in BUILD.bazel is fine. For CI/CD, use Bazel flags or stamp variables.

## OCI Distribution Protocol

HTTP-based REST API. Agents can use curl, Python, or tools like `crane`.

Repository names (`<name>`) are agent types: `critic`, `grader`, `prompt-optimizer`.

### Pull (read)

```
GET /v2/<name>/manifests/<reference>     # Get image manifest (by tag or digest)
GET /v2/<name>/blobs/<digest>            # Get layer blob
HEAD /v2/<name>/manifests/<reference>    # Check if image exists
GET /v2/<name>/tags/list                 # List tags for an image
GET /v2/_catalog                         # List all repositories
```

### Push (write)

```
# 1. Upload layer blob
POST /v2/<name>/blobs/uploads/           # Start upload, get upload URL
PATCH <upload-url>                       # Stream blob data
PUT <upload-url>?digest=sha256:...       # Finish upload

# 2. Upload manifest BY DIGEST (not tag - proxy blocks tag writes)
PUT /v2/<name>/manifests/sha256:...      # Push manifest by digest
```

### Example: Layer on Existing Image

Agents use Python (aiodocker or httpx/requests) to interact with the registry.

```python
import hashlib
import httpx

PROXY = "http://registry-proxy:5050"

# 1. Get base image manifest
resp = httpx.get(
    f"{PROXY}/v2/critic/manifests/builtin",
    headers={"Accept": "application/vnd.oci.image.manifest.v1+json"}
)
base_manifest = resp.json()

# 2. Upload new layer blob
layer_data = create_tar_layer(files_to_add)
layer_digest = f"sha256:{hashlib.sha256(layer_data).hexdigest()}"

# Start upload
upload_resp = httpx.post(f"{PROXY}/v2/critic/blobs/uploads/")
upload_url = upload_resp.headers["Location"]

# Finish upload with digest
httpx.put(f"{upload_url}?digest={layer_digest}", content=layer_data)

# 3. Create new manifest referencing base layers + new layer
new_manifest = {
    **base_manifest,
    "layers": base_manifest["layers"] + [{"digest": layer_digest, ...}]
}
manifest_bytes = json.dumps(new_manifest).encode()
manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

# 4. Push manifest BY DIGEST
httpx.put(
    f"{PROXY}/v2/critic/manifests/{manifest_digest}",
    content=manifest_bytes,
    headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"}
)

# manifest_digest is what gets recorded in agent_run.image_digest
```

Using curl (for reference/debugging):

```bash
# Get manifest of builtin image
curl -H "Accept: application/vnd.oci.image.manifest.v1+json" \
  http://registry-proxy:5050/v2/critic/manifests/builtin

# Start blob upload
curl -X POST http://registry-proxy:5050/v2/critic/blobs/uploads/

# Upload blob (monolithic)
curl -X PUT "http://registry-proxy:5050/v2/critic/blobs/uploads/<uuid>?digest=sha256:..." \
  --data-binary @layer.tar

# Push manifest BY DIGEST (compute sha256 of manifest.json first)
MANIFEST_DIGEST=$(sha256sum manifest.json | cut -d' ' -f1)
curl -X PUT "http://registry-proxy:5050/v2/critic/manifests/sha256:$MANIFEST_DIGEST" \
  -H "Content-Type: application/vnd.oci.image.manifest.v1+json" \
  -d @manifest.json
```

### Tools

- **aiodocker** (Python, already a props dep) - async Docker/registry API, natural for agents
- **Python requests/httpx** - direct HTTP to OCI endpoints, no extra dependencies
- **curl** - available in containers, verbose but works

Note: crane/skopeo are useful for local dev/debugging but won't be bundled in agent containers.

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

## Migration Plan

### Phase 1: Devenv Infrastructure

Update `props/devenv.nix` to manage all three services:

- [ ] Create two Docker networks:
  - `props-internal`: registry + proxy + postgres (not accessible to agents)
  - `props-agents`: proxy + postgres + agent containers
- [ ] Add registry container on `props-internal` only (agents cannot reach it directly)
- [ ] Add registry proxy container on both networks (bridges agents to registry)
- [ ] Ensure postgres, registry, and proxy all start together via process-compose
- [ ] Agent containers join `props-agents` network, can only reach `registry-proxy:5050`

### Phase 2: Registry Proxy Implementation

Implement `props/registry_proxy/` - FastAPI service built as Bazel OCI image.

**Bootstrap sequence (chicken-and-egg):**

The proxy image must exist before the proxy can enforce pushes. Bootstrap:

1. Build proxy image locally: `bazelisk run //props/registry_proxy:load`
2. Push directly to registry (bypassing proxy): `docker tag ... && docker push localhost:5000/registry-proxy:builtin`
3. Start proxy container (now it can enforce all subsequent pushes)
4. Future proxy updates: push through proxy with admin auth like any other builtin

After initial bootstrap, all pushes (including proxy updates) go through the proxy.

**Implementation:**

- [ ] Create `props/registry_proxy/BUILD.bazel` with `oci_image` target
- [ ] Add bootstrap script for initial proxy push (direct to registry)
- [ ] Devenv pulls and runs proxy container from registry

The proxy:

- [ ] Proxies OCI Distribution Protocol requests to the registry
- [ ] Two auth modes (both validated against postgres):
  - **Agent auth**: agent temp users, digest-only pushes
  - **Admin auth**: postgres admin user, allows tag pushes for builtins
- [ ] Enforces ACL (agent mode):
  - Only prompt-optimizer and prompt-improver agents can read/upload images
  - Critic/grader never touch registry - launch infrastructure (`agent_setup.py`) pulls for them
  - No DELETE operations (block all)
  - No tag writes (block `PUT /v2/<name>/manifests/<tag>`, allow only `PUT /v2/<name>/manifests/sha256:...`)
- [ ] Writes `agent_definitions` row on every manifest push (both agent and admin modes)
- [ ] Returns appropriate errors for blocked operations

### Phase 3: Schema and API Migration

Update agent definition references to use docker digests. DB drop-recreate is acceptable (no backward compatibility needed).

- [ ] Rename `agent_runs.agent_definition_id` → `agent_runs.image_digest`
- [ ] Redefine `agent_definitions` table (same concept, new structure):
  ```sql
  CREATE TABLE agent_definitions (
    digest VARCHAR PRIMARY KEY,            -- sha256:... (immutable identifier)
    agent_type agent_type_enum NOT NULL,   -- keeps existing enum (critic, grader, etc.)
    created_by_agent_run_id UUID,          -- which agent created this (nullable for builtin)
    base_digest VARCHAR,                   -- parent image digest if layered
    created_at TIMESTAMP DEFAULT NOW()
    -- removed: id (text), archive (bytea)
  );
  ```
- [ ] `agent_runs.image_digest` is FK to `agent_definitions.digest`
- [ ] Keep `agent_type_enum` - unchanged
- [ ] Keep `type_config->>'agent_type'` in agent_runs - unchanged (used by RLS)
- [ ] Update RLS policies that reference `agent_definitions.id` to use `.digest`
- [ ] Update agent-launching MCP APIs (that PO/PI have access to) to:
  - Accept image digest parameter for launching agents
  - Record digest in `agent_runs.image_digest`
- [ ] Update agent launch code (`props/core/agent_setup.py`) to pull by digest

### Phase 4: Documentation Updates

Update all agent-related documentation:

- [ ] Update `props/core/docs/authoring_agents.md.j2`:
  - New flow: pull builtin by tag → layer → push by digest
  - How to use Python/httpx for layering
  - How image digests are recorded in agent_runs
  - ACL: only PO/PI can read/write images
- [ ] Update `props/core/agent_defs/*/agent.md` - per-agent specifics
- [ ] Update `props/core/docs/agent_infrastructure.md` - infrastructure overview
- [ ] Add layering recipes and examples to agent prompts

### Phase 5: Migrate Other Packages

**`agent_pkg/host/`:**

- [ ] Update `builder.py` to pull via proxy instead of `docker buildx build`
- [ ] Remove `ensure_image_from_archive()` (no more tarball builds)
- [ ] Update `init_runner.py` to work with proxy-pulled images

**`editor_agent/`:**

- [ ] Create Bazel `oci_image` + `oci_push` targets for editor agent
- [ ] Push to registry via proxy as `editor:builtin`
- [ ] Update `agent_runner.py` to pull via proxy

### Phase 6: Cleanup

Remove legacy tarball-based agent packaging:

**Database:**

- [ ] Redefine `agent_definitions` table (digest-based, see Phase 3)
- [ ] Change `agent_runs.agent_definition_id` → `agent_runs.image_digest` (FK to agent_definitions.digest)

**Core library (`props/core/`):**

- [ ] `db/agent_definition_ids.py` - remove or repurpose (constants become image refs)
- [ ] `agent_registry.py` - update `definition_id` params to `image_digest`
- [ ] `agent_setup.py` - remove tarball extraction, add proxy-based pull
- [ ] `agent_pkg_utils.py` - remove `pack_agent_pkg`, `unpack_agent_pkg` if no longer needed
- [ ] `cli/cmd_agent_pkg.py` - remove tarball CLI commands (`fetch`, `create`)
- [ ] `db/sync/sync.py` - remove agent_definitions sync logic

**Agent definitions (`props/core/agent_defs/`):**

- [ ] Remove `Dockerfile` from each agent_def (images built by Bazel)
- [ ] Keep `agent.md` files (documentation)
- [ ] Add `oci_push` targets to each agent_def's BUILD.bazel
- [ ] Update `props/core/BUILD.bazel` data glob (remove Dockerfiles from glob)

**Documentation:**

- [ ] `docs/authoring_agents.md.j2` - rewrite for OCI image workflow
- [ ] `docs/db/agent_definitions.md.j2` - remove or rewrite

**Note:** Registry GC deferred - not critical for MVP

## Files to Update

### New Files

| File                    | Purpose                                  |
| ----------------------- | ---------------------------------------- |
| `props/registry_proxy/` | New proxy service (FastAPI, BUILD.bazel) |

### Modified Files

| File                                             | Changes                                         |
| ------------------------------------------------ | ----------------------------------------------- |
| `props/devenv.nix`                               | Add registry, proxy containers, Docker network  |
| `props/core/db/models.py`                        | `image_digest` column, remove agent_definitions |
| `props/core/agent_setup.py`                      | Pull via proxy instead of tarball extraction    |
| `props/core/agent_registry.py`                   | `definition_id` → `image_digest` params         |
| `props/core/prompt_optimize/prompt_optimizer.py` | MCP tools to launch agents by digest            |
| `props/core/prompt_improve/improve_agent.py`     | MCP tools to launch agents by digest            |
| `props/core/agent_defs/*/BUILD.bazel`            | Add `oci_push` targets                          |
| `props/core/BUILD.bazel`                         | Update data glob for agent_defs                 |
| `props/core/docs/authoring_agents.md.j2`         | Rewrite for OCI image workflow                  |

### Files to Remove

| File                                         | Reason                               |
| -------------------------------------------- | ------------------------------------ |
| `props/core/db/agent_definition_ids.py`      | Constants replaced by image refs     |
| `props/core/agent_pkg_utils.py`              | Tarball pack/unpack no longer needed |
| `props/core/cli/cmd_agent_pkg.py`            | Tarball CLI no longer needed         |
| `props/core/agent_defs/*/Dockerfile`         | Images built by Bazel                |
| `props/core/docs/db/agent_definitions.md.j2` | Table removed                        |

### Other Packages to Migrate

**`agent_pkg/` (agent package infrastructure):**
| File | Changes |
|------|---------|
| `agent_pkg/host/builder.py` | Replace `docker buildx build` with proxy-based pull |
| `agent_pkg/host/init_runner.py` | Pull by digest via proxy instead of local image tag |

The `ensure_image_from_archive()` function becomes unnecessary - images are pre-built and pulled via proxy.

**`editor_agent/` (editor agent):**
| File | Changes |
|------|---------|
| `editor_agent/` | Migrate from Dockerfile in repo to Bazel OCI image |
| `editor_agent/host/agent_runner.py` | Use proxy-based pull instead of local build |

Editor agent currently builds from a local Dockerfile. Needs:

- Bazel `oci_image` + `oci_push` targets
- Push via proxy as `editor:builtin`
- Update runner to pull via proxy

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

### ✅ Completed

**Phase 1: Schema Migration**

- ✅ `agent_definitions` table migrated to digest-based primary key
- ✅ `agent_runs.agent_definition_id` → `agent_runs.image_digest`
- ✅ All tarball support removed (archive column, build functions)
- ✅ 16 files updated across props/core codebase

**Phase 2: Registry Infrastructure**

- ✅ OCI registry container configured in devenv.nix (port 5050)
- ✅ Registry proxy implemented with FastAPI (props/registry_proxy/proxy.py)
- ✅ Postgres credential validation via connection testing (\_validate_postgres_credentials)
  - ✅ Basic auth validates admin user against postgres (line 96)
  - ✅ Bearer token validates agent*{run_id}*{password} against postgres (line 128)
- ✅ ACL enforcement (admin/PO/PI can push, critic/grader cannot)
- ✅ Agent definitions tracking (writes to DB on manifest push)
- ✅ Proxy OCI image BUILD targets (load/push)
- ✅ Network isolation (props-internal for registry, props-agents for agent access)
- ✅ Proxy container startup in devenv.nix (auto-builds image, health checks, connects networks)
- ✅ Deprecated tarball stubs deleted (commit 61aac4f1)

**Phase 3: Agent Builds**

- ✅ Critic agent bazelized (BUILD.bazel with oci_image, oci_push)
- ✅ Grader agent bazelized
- ✅ Prompt-optimizer agent bazelized
- ✅ All agents build successfully

**Network Configuration**

- ✅ `PROPS_NETWORK_NAME` updated to `props-agents`
- ✅ Agent containers connect to props-agents network
- ✅ Postgres accessible from both networks (props-internal + props-agents)
- ✅ Proxy bridges both networks for ACL enforcement

### 🚧 In Progress / Remaining Gaps

**Critical: Agent Launch Integration**

- ❌ Agent environments still use legacy `definition_id` parameter
- ❌ Need to add `image_ref` parameter to environment constructors
- ❌ `AgentRegistry` launch methods need tag resolution
- ❌ Need to pass resolved digest to `AgentEnvironment`
- **Blocker**: Agent launches will fail with "image_ref required" error

**Tag Resolution System Design**

Agents should be launchable by convenient tags (`critic:builtin`) while storing immutable digests in the database.

**Function:** `resolve_image_ref(repository: str, ref: str) -> str`

```python
def resolve_image_ref(repository: str, ref: str) -> str:
    """Resolve image reference to digest.

    Args:
        repository: Repository name (e.g., "critic", "grader")
        ref: Tag or digest (e.g., "builtin", "sha256:abc...")

    Returns:
        Digest (sha256:...) - either the provided digest or resolved from tag

    Raises:
        ValueError: If tag doesn't exist or proxy returns error
    """
    # If already a digest, return as-is
    if _is_digest(ref):
        return ref

    # Resolve tag via proxy HEAD request (admin auth)
    # HEAD /v2/{repository}/manifests/{tag}
    # Response header: Docker-Content-Digest: sha256:...

    proxy_url = f"http://localhost:5050/v2/{repository}/manifests/{ref}"
    headers = {"Accept": "application/vnd.oci.image.manifest.v1+json"}

    # Use admin auth from environment (PGUSER/PGPASSWORD)
    auth = (os.environ["PGUSER"], os.environ["PGPASSWORD"])

    resp = requests.head(proxy_url, headers=headers, auth=auth)
    if resp.status_code == 404:
        raise ValueError(f"Image not found: {repository}:{ref}")
    resp.raise_for_status()

    digest = resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise ValueError(f"Proxy didn't return digest for {repository}:{ref}")

    return digest
```

**Usage in launch flow:**

```python
# props/core/agent_registry.py
def run_critic(
    snapshot: Snapshot,
    image_ref: str = "builtin",  # Tag or digest
    ...
) -> CriticRun:
    # Resolve tag to digest
    image_digest = resolve_image_ref("critic", image_ref)

    # Create run with digest (immutable reference)
    run = CriticRun(image_digest=image_digest, ...)
    session.add(run)
    session.flush()

    # Launch with resolved digest
    env = CriticAgentEnvironment(
        run_id=run.run_id,
        image_digest=image_digest,  # New parameter
        ...
    )
    await env.start()
```

**Implementation tasks:**

- ❌ Add `resolve_image_ref()` to props/core/docker_env.py or new props/core/oci_utils.py
- ❌ Update `CriticAgentEnvironment.__init__()` to accept `image_digest` parameter
- ❌ Update `GraderAgentEnvironment.__init__()` to accept `image_digest` parameter
- ❌ Update `PromptOptimizerEnvironment.__init__()` to accept `image_digest` parameter
- ❌ Update `AgentRegistry.run_critic()` to call resolve_image_ref()
- ❌ Update `AgentRegistry.run_grader()` to call resolve_image_ref()
- ❌ Update test fixtures to pass `image_digest="sha256:test..."` or mock resolve_image_ref()

**Runtime Testing**

- ❌ Test `devenv up` starts all containers (postgres + registry + proxy)
- ❌ Verify network isolation works
- ❌ Test `bazel run //props/core/agent_defs/critic:push` succeeds
- ❌ Run e2e tests (critic/grader/prompt-optimizer)
- **Blocker**: Need running registry + launch integration

**Agent Token Generation**

- ❌ Generate bearer tokens when creating temp DB users
- ❌ Token format: `agent_{run_id}_{password}`
- ❌ Pass tokens to agent containers via env vars
- ❌ Configure Docker registry auth in containers

**Additional Agent Builds** (Lower Priority)

- ❌ `contract_truthfulness/` (critic-based detector)
- ❌ `dead_code/` (critic-based detector)
- ❌ `flag_propagation/` (critic-based detector)
- ❌ `high_recall_critic/` (critic-based detector)
- ❌ `improvement/` (prompt improver agent)
- ❌ `verbose_docs/` (unclear if agent)

**Future Optimization**

- ❌ Common base image with pre-installed Python packages
- ❌ Would reduce image size duplication and speed up builds
- **Note**: Documented in `props/core/agent_defs/AGENTS.md`

### Estimated Completion

- **Current**: ~70-75% complete
- **Next critical task**: Agent launch integration (tag resolution + image_ref parameter)
- **After that**: Runtime testing with full stack

### Key Design Decisions Made

1. **Network isolation enforced**: Two Docker networks prevent ACL bypass
2. **Postgres validates all auth**: No hardcoded credentials, all validated against DB
3. **Digest-only in database**: Tags convenient for launching, digests for immutability
4. **Proxy auto-builds on devenv up**: No manual setup required
5. **Migration path preserved**: Legacy `definition_id` can map to tags during transition
