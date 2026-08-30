# Home Assistant HTTP-grant migration

This document describes which parts of Haku's Home Assistant access can move from
`homeassistant-ai/ha-mcp` to the Haku egress proxy, and which parts need a
payload-aware adapter first.

## Current wiring

The `home_assistant_reads` auto-approval policy grants Haku a fixed set of
read-only MCP tools. Mutating and control tools remain available through the
same MCP server, but are operator-gated. The policy is defined in
`cluster/k8s/haku/console/config.yaml`.

The HA-MCP deployment currently keeps `HOMEASSISTANT_TOKEN` in the HA-MCP pod.
Haku authenticates to the MCP facade with a separate static bearer. An HTTP
migration would therefore be a credential-placement change as well as a tool
catalog change: the HA token would have to become a Console egress credential,
while the sandbox would receive only its inert placeholder.

## First migration slice

The following capabilities are suitable for a first, GET-only migration after
the routes are verified against the pinned HA-MCP `7.14.0` implementation and
the installed Home Assistant version:

| MCP capability | Candidate Home Assistant route | Notes |
| --- | --- | --- |
| `ha_get_state` | `/api/states` and `/api/states/{entity_id}` | The current tool accepts arbitrary entity IDs. A GET grant for these routes is close to equivalent. |
| `ha_get_history` | `/api/history/period...` | Query parameters remain unconstrained by an HTTP path regex, but the current read tool already permits caller-selected history windows and entities. |
| Logbook portion of `ha_get_logs` | `/api/logbook...` | Read-only route. |
| Error-log portion of `ha_get_logs` | `/api/error_log` | Read-only route. |
| `ha_list_services` | `/api/services` | Service discovery only; do not grant the corresponding service-call routes. |
| `ha_get_camera_image` | `/api/camera_proxy/{entity_id}` | Verify response shape and route behavior before removing the MCP tool. |

Calendar reads may be a later addition if the deployed HA API and HA-MCP
implementation use the expected `/api/calendars/{entity_id}` route. They are
not part of the initial migration because the exact version-specific behavior
needs a live compatibility test.

The initial grant should be one exact internal origin, `GET` only, and a
full-match path expression. Conceptually:

```yaml
- id: haku-ha-rest-reads
  principal: { kind: agent, agent_id: <haku-agent-id> }
  origins:
    - scheme: http
      host: home-assistant.home-assistant.svc.cluster.local
      port: 8123
  coverage:
    methods: [GET]
    path_regex: '^/api/(states(?:/[^/?]+)?|history/period(?:/.*)?|logbook(?:/.*)?|error_log|services|camera_proxy/[^/?]+)(?:\?.*)?$'
  credential_handle: home-assistant-api
  allow_prohibited_address: true
```

This is illustrative rather than deploy-ready. The real change must add the
credential registry entry, its Secret-backed value environment variable, the
standing grant, and the sandbox's inert placeholder as one reviewed change.
The HA-MCP read tools must be removed from `home_assistant_reads` in the same
rollout; otherwise the two surfaces combine and the result is wider than the
intended migration.

## Why the grant must stay narrow

HTTP grants constrain the exact origin, method set, and path-plus-query regex.
They do not constrain request bodies, response fields, JSON selectors, or
WebSocket messages. In particular:

- Do not grant `/api/*` or all `GET` requests at the HA origin. That would expose
  unrelated HA endpoints not represented by the auto-approved tool set.
- Do not grant `POST /api/services/...`. The service, target entities, and data
  are controlled by the request body, so a path-only grant would be control
  authority rather than a read equivalent.
- Do not grant `/api/websocket` as a replacement for registry/configuration
  reads. HA multiplexes read and write commands over that endpoint, and the
  current egress matcher cannot inspect WebSocket payloads.
- Keep `allow_prohibited_address: true` limited to this exact HA service origin.
  It is required for a cluster-internal destination and must not become a
  general private-address exception.

## Capabilities that stay on MCP for now

These current auto-approved reads should remain on HA-MCP until a more specific
adapter exists:

- entity/device/area/floor/label/zone/helper/integration registries;
- automation, script, scene, blueprint, dashboard-resource, and trace reads;
- todo reads and system-health/operation-status reads;
- add-on and Supervisor information;
- HACS information and the composite `ha_search` and `ha_get_overview` tools;
- `ha_get_skill_guide`, which is MCP-provided documentation rather than an HA
  API route.

Most of these are composite operations or use HA's WebSocket/config-registry
APIs. A raw route grant would either fail to reproduce the tool or authorize a
shared endpoint that also carries mutations.

## Feasible migration of the remaining reads

The remaining read surface can migrate, but not by adding broad route grants.
Use one of these designs:

1. **Dedicated HA read facade.** Add a small service with one endpoint per
   semantic operation, for example `/read/entity-registry`,
   `/read/automation/{id}`, or `/read/system-health`. The facade owns the HA
   token, permits only read operations, validates path/query inputs, and
   translates the result into a stable response. Grant Haku `GET` access only
   to those exact endpoints. This is the preferred design for WebSocket-backed
   reads.
2. **Payload-aware egress policy.** Extend the proxy decision contract to carry
   a reviewed request shape and have the policy evaluator validate method,
   path, query, and selected JSON body fields. This could safely support a
   small allowlist of WebSocket commands, but it is a larger security-sensitive
   change and should not be implemented as a generic "allow HA WebSocket"
   switch.
3. **Per-operation MCP wrappers.** Keep HA-MCP as the upstream implementation,
   but expose separate read-only facade endpoints that invoke only approved
   operations. This preserves the existing HA-MCP compatibility logic while
   moving Haku's network capability to explicit route-level grants.

The facade approach is also needed for read tools whose result is assembled
from multiple HA endpoints, such as `ha_search` and `ha_get_overview`, or whose
upstream API requires Supervisor/HACS credentials rather than the ordinary HA
 token.

## Rollout and verification

Before removing any MCP tools:

1. Add the Secret-backed `home-assistant-api` egress credential and a distinct
   inert placeholder. Never commit the HA token or place it in the sandbox.
2. Add the exact-origin standing grant and confirm the internal-origin
   allowance is scoped only to Haku's Agent principal.
3. From a real Haku sandbox, exercise each candidate route through the Haku
   proxy with the placeholder and verify that the upstream sees the substituted
   bearer.
4. Verify that an uncovered path, every non-GET method, `/api/websocket`, and
   `/api/services/*` are denied by the proxy.
5. Compare representative responses with the MCP tool responses, including
   empty results, unavailable entities, query parameters, and large payloads.
6. Remove the migrated tools from `home_assistant_reads`, deploy, and perform a
   fresh Haku session test. Only then remove unused HA-MCP read configuration.

The token move should be called out in the deployment review: the secret remains
outside the Agent, but its redemption authority moves from the isolated HA-MCP
pod to the Haku egress-fence credential registry.
