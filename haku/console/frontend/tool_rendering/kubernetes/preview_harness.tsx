import { mountPreviewCards } from "../screenshot/mount";

import type { RegisteredToolPreviewFixture } from "../index";
import type { McpToolArgumentsFor } from "../../mcp_tool_schema";
import type { McpToolResultFor } from "../../mcp_tool_result_schema";

type CreateGrantArgs = McpToolArgumentsFor<"kubernetes", "create_grant">;

const GRANT: CreateGrantArgs["grants"][number] = {
  scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox", "haku-staging"] },
  rules: [
    { api_groups: [""], resources: ["pods"], verbs: ["get", "list"], resource_names: [] },
    { api_groups: ["apps"], resources: ["deployments"], verbs: ["patch"] },
  ],
};

const RESULT_GRANT: McpToolResultFor<"kubernetes", "release_grants">[number] = {
  grant_id: "20000000-0000-4000-8000-000000000002",
  owner_agent_id: "10000000-0000-4000-8000-000000000001",
  principal: { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" },
  source_tool_call_id: "tc_create_grant",
  scope: GRANT.scope,
  rules: GRANT.rules,
  status: "active" as const,
  created_at: "2026-08-23T10:00:00Z",
  expires_at: "2026-08-23T11:00:00Z",
};

const PREVIEW_FIXTURES = [
  {
    title: "Create temporary Kubernetes grants",
    serverId: "kubernetes",
    toolName: "create_grant",
    args: { grants: [GRANT], duration_seconds: 3600 },
    result: [RESULT_GRANT],
  },
  {
    title: "Release several Kubernetes grants",
    serverId: "kubernetes",
    toolName: "release_grants",
    args: {
      grant_ids: [RESULT_GRANT.grant_id, "20000000-0000-4000-8000-000000000003"],
      reason: "probe complete",
    },
    result: [
      {
        ...RESULT_GRANT,
        status: "released" as const,
        ended_at: "2026-08-23T10:15:00Z",
        end_reason: "probe complete",
      },
    ],
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
