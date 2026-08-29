import { mountPreviewCards } from "../screenshot/mount";

import type { RegisteredToolPreviewFixture } from "../index";
import type { McpToolArgumentsFor } from "../../mcp_tool_schema";
import type { McpToolResultFor } from "../../mcp_tool_result_schema";

type CreateGrantItem = McpToolArgumentsFor<"grants", "create_grant">["grants"][number];
type GrantView = McpToolResultFor<"grants", "revoke_grants">[number];

const KUBERNETES_ITEM: CreateGrantItem = {
  domain: "kubernetes" as const,
  spec: {
    scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox", "haku-staging"] },
    rules: [
      { api_groups: [""], resources: ["pods"], verbs: ["get", "list"], resource_names: [] },
      { api_groups: ["apps"], resources: ["deployments"], verbs: ["patch"] },
    ],
  },
};

const HTTP_ITEM: CreateGrantItem = {
  domain: "http" as const,
  spec: {
    origin: { scheme: "https" as const, host: "api.github.com", port: 443 },
    coverage: { methods: ["GET", "POST"], path_regex: "/repos/agentydragon/.*" },
    credential_handle: "github-api",
  },
};

// Keep these timestamps inside the shared visual clock's relative-date window. The screenshot
// driver freezes now at 2025-02-01T12:00:00Z before this harness runs.
const KUBERNETES_VIEW: GrantView = {
  domain: "kubernetes" as const,
  grant: {
    grant_id: "20000000-0000-4000-8000-000000000002",
    owner_agent_id: "10000000-0000-4000-8000-000000000001",
    principal: { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" },
    source_tool_call_id: "tc_create_grant",
    scope: KUBERNETES_ITEM.spec.scope,
    rules: KUBERNETES_ITEM.spec.rules,
    status: "active" as const,
    created_at: "2025-01-27T10:00:00Z",
    expires_at: "2025-01-27T11:00:00Z",
  },
};

const PREVIEW_FIXTURES = [
  {
    title: "Create temporary Kubernetes grants",
    serverId: "grants",
    toolName: "create_grant",
    args: { grants: [KUBERNETES_ITEM], duration_seconds: 3600 },
    result: [KUBERNETES_VIEW],
  },
  {
    title: "Create a temporary HTTP egress grant",
    serverId: "grants",
    toolName: "create_grant",
    args: { grants: [HTTP_ITEM], duration_seconds: 1800 },
    result: [
      {
        domain: "http" as const,
        grant: {
          grant_id: "20000000-0000-4000-8000-000000000004",
          owner_agent_id: "10000000-0000-4000-8000-000000000001",
          principal: { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" },
          source_tool_call_id: "tc_create_grant",
          spec: HTTP_ITEM.spec,
          status: "active" as const,
          created_at: "2025-01-27T10:00:00Z",
          expires_at: "2025-01-27T10:30:00Z",
        },
      },
    ],
  },
  {
    title: "Release several Kubernetes grants (Agent)",
    serverId: "grants",
    toolName: "revoke_grants",
    args: {
      domain: "kubernetes" as const,
      grant_ids: [KUBERNETES_VIEW.grant.grant_id, "20000000-0000-4000-8000-000000000003"],
      reason: "probe complete",
    },
    result: [
      {
        domain: "kubernetes" as const,
        grant: {
          ...KUBERNETES_VIEW.grant,
          status: "released" as const,
          released_at: "2025-01-27T10:15:00Z",
          revoked_at: null,
          end_reason: "probe complete",
        },
      },
    ],
  },
  {
    title: "Revoke an owned Agent's grant (Operator)",
    serverId: "grants",
    toolName: "revoke_grants",
    args: {
      domain: "http" as const,
      owner_agent_id: KUBERNETES_VIEW.grant.owner_agent_id,
      grant_ids: ["20000000-0000-4000-8000-000000000004"],
      reason: "operator revoked",
    },
    result: [
      {
        domain: "http" as const,
        grant: {
          grant_id: "20000000-0000-4000-8000-000000000004",
          owner_agent_id: KUBERNETES_VIEW.grant.owner_agent_id,
          principal: { kind: "agent" as const, agent_id: KUBERNETES_VIEW.grant.owner_agent_id },
          source_tool_call_id: "tc_create_grant",
          spec: HTTP_ITEM.spec,
          status: "revoked" as const,
          created_at: "2025-01-27T10:00:00Z",
          expires_at: "2025-01-27T11:00:00Z",
          released_at: null,
          revoked_at: "2025-01-27T10:20:00Z",
          end_reason: "operator revoked",
        },
      },
    ],
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
