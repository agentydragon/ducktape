# Suppress Redundant Kubectl Passthrough Approvals

**Status:** design plan; ready for implementation.

## Overview

Issue #4590 requests suppressing redundant operator approval prompts for `kubectl-passthrough-mcp` tool calls originating from an Agent when its direct Haku Kubernetes proxy permissions (standing SAR plus active temporary grants) already cover the required access.

When an Agent with a configured Kubernetes authorization profile calls a tool on `kubectl-passthrough-mcp`:

1. The console intercepts the call at submission time (`ToolCallApplicationService.submit_and_wait`).
2. It maps the specific tool name and validated arguments to a set of canonical `AuthorizationRequest` definitions.
3. It evaluates each request against the existing `KubernetesAuthorizationService` (standing SAR first, then active Agent-owned temporary grants).
4. **If all required requests are allowed:** The call is denied immediately at submission with a clear explanation directing the Agent to use its direct Haku Kubernetes proxy or local `kubectl` route instead. No pending approval row or notification is created.
5. **If any required request is denied, unmappable, unknown, or the evaluator raises a transient unavailability error:** The system falls back safely to the ordinary approval-gated passthrough call.
6. **Operator-originated calls:** Unaffected (operators use direct execution paths that bypass `submit_and_wait`).

---

## Detailed Mapping Scope

### 1. Read Tools (Unambiguous Get/List)

- **`pods_list`**: `list` on `pods` (API group `""`). Namespace-scoped if `namespace` is provided; `all_namespaces` scope otherwise.
- **`pods_get`**: `get` on `pods` (API group `""`). Name and namespace (defaults to `"default"`).
- **`nodes_list`**: `list` on `nodes` (API group `""`), cluster-scoped.
- **`nodes_get`**: `get` on `nodes` (API group `""`), cluster-scoped.
- **`resources_list`**: Parses `apiVersion` (group + version) and `kind` (mapped to plural resource name), `namespace`. `list` verb.
- **`resources_get`**: Parses `apiVersion`, `kind`, `name`, `namespace`. `get` verb.
- **`events_list`**: `list` on `events` (API group `""`).

### 2. Specialized Subresources

- **`pods_log`**: Models `pods/log` subresource (`get` verb on `pods/log`, name and namespace).
- **`pods_exec`**: Models `pods/exec` subresource (`create` verb on `pods/exec`, plus `get` verb on `pods`, name and namespace).

### 3. Mutations & Applies

- **`pods_delete`**: `delete` verb on `pods`, name and namespace.
- **`resources_delete`**: `delete` verb on resource/kind, name and namespace.
- **`resources_create_or_update`**: Parses the YAML/JSON manifest string in `resource` to extract `apiVersion`, `kind`, `metadata.name`, and `metadata.namespace`. Generates an explicit sequence of required requests:
  1. `get` on the resource name (to check existence).
  2. `create` on the resource (to create if missing).
  3. `patch` on the resource name (to update if existing).
     All three must be covered by direct permissions for the apply call to be auto-bypassed.

### 4. Unmapped Operations

- Any unknown or unmappable tool name / argument shape returns `None`, safely falling back to ordinary operator approval.

---

## Implementation Steps

1. **Service Wiring**:
   - Update `ToolCallApplicationService.__init__` to accept an optional `kubernetes_authorization: KubernetesAuthorizationService | None = None`.
   - Wire `kubernetes_authorization` in `haku/console/app.py`.

2. **Mapping & Evaluation Logic**:
   - Implement `haku/console/kubectl_passthrough_policy.py` containing the mapper and evaluator helper.
   - Integrate the check into `ToolCallApplicationService.submit_and_wait` for `kubectl-passthrough-mcp` tool calls from `AgentActor` with `access_profile_id`.

3. **Testing**:
   - Add comprehensive unit tests in `haku/console/test_kubectl_passthrough_policy.py` and `haku/console/test_tool_call_service.py` covering:
     - Fully covered read/write/exec calls (denied at submission with direct-route advice).
     - Partially covered or denied calls (fall through to normal pending approval).
     - Unmapped tools and schema drift (fall through to normal pending approval).
     - Evaluator timeout / unavailability (fall through to normal pending approval).
     - Operator callers (unaffected).
