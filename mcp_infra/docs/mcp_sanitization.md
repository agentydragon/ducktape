# MCP Server Sanitization

**Status:** Phase 2.10 Partial ✅ | Phase 2.9, 3-4 Future
**Updated:** 2025-12-18
**Last major milestone:** Phase 2.8 complete (PropertiesDockerWiring eliminated)

## Completed Phases

### Phase 1 ✅

- All 21 MCP servers migrated to `EnhancedFastMCP` with typed tool attributes

### Phase 2.1-2.6 ✅

- Six compositor recipes implemented (Lint, Critic, Grader, GitCommit, Matrix, AgentContainer)
- API: `Compositor.mount_inproc()` returns `Mounted[T]` for type-safe server access
- Base `Compositor` auto-mounts `resources` and `compositor_meta`

### Phase 2.7 ✅

- `PropertiesDockerWiring` returns `Mounted[ContainerExecServer]`
- Bootstrap helpers updated to accept typed mounts
- OpenAI strict mode fix for lint models (uses `anyOf` schema)

### Phase 2.8 ✅

- `PropertiesDockerWiring` eliminated entirely
- `PropertiesDockerCompositor` intermediate class replaces wiring pattern
- Three properties compositors (Critic, Grader, Lint) inherit from `PropertiesDockerCompositor`
- All Docker logic centralized; no duplication across compositors

## Remaining Work

### Phase 2.9: Eliminate Mount Prefix/Tool Name Constants (Blocked)

**Status:** 🔄 Blocked - Requires initialization order reorganization
**Issue:** Many constant uses occur before compositor mounts are available

- `approval_policy/engine.py` renders templates during **init** (before servers mounted)
- `agent/policies/default_policy.py` executes in Docker isolation (no compositor access)
- `agent/server/reducer.py` needs UI tool names during session initialization
- Test infrastructure uses class-level constants (acceptable exception)

**Decision:** Not a priority. Blocked pending architectural refactoring of initialization order.

### Phase 2.10: Resource URIs as Typed Server Attributes (Partial)

**Status:** 🔄 Partial - ContainerExecServer complete, others remain
**Completed:** `ContainerExecServer.container_info_resource` (FunctionResource)
**Remaining:** CompositorMetaServer, PolicyServer, ApprovalPolicyServer URI attributes

**Goal:** Move resource URIs from `constants.py` to typed server attributes (parallel to tool access)

**Implementation reference:** `mcp_infra/exec/docker/server.py`

- Use `_resource` suffix to distinguish from function names
- Cast to `FunctionResource` (static URI) or `FunctionResourceTemplate` (parameterized)
- Access via `server.container_info_resource.uri`

**Example URIs to migrate:**

- `COMPOSITOR_META_STATE_URI_FMT` → `CompositorMetaServer.server_state_resource`
- `CONTAINER_INFO_URI` → ✅ Done
- `POLICY_RESOURCE_URI` → `PolicyServer.policy_resource`
- `UI_STATUS_URI` → `UiServer.status_resource`

### Phase 3: Test Fixture Migration

- Update test fixtures to use compositor subclasses where beneficial
- Convert remaining manual mock setups to typed compositors

### Phase 4: Final Validation & Cleanup

- Run full test suite and type check
- Verify no new string literals in production code
